"""Start isolated HTTP fixtures and two gateway replicas, run E2E, clean up.

Requires an existing dedicated Redis socket/URL. Does not flush Redis or stop any
existing process. Every run uses a new quota namespace and free TCP ports.
"""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import httpx
import yaml


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main(redis_url):
    root = Path(__file__).resolve().parents[1]
    mock_port, first, second = (free_port() for _ in range(3))
    run_id = uuid.uuid4().hex
    output = root / '.runs' / 'e2e' / run_id
    output.mkdir(parents=True)
    processes, logs = [], []
    with tempfile.TemporaryDirectory(prefix='gateway-e2e-') as temporary:
        config = yaml.safe_load((root / 'config.demo.yaml').read_text())
        for name, route in config['platform']['routes'].items():
            route['base_url'] = f"http://127.0.0.1:{mock_port}/{route['provider']}/v1"
        config['platform']['redis_url'] = redis_url
        config['platform']['quota_namespace'] = 'gateway-e2e-' + run_id
        config_path = Path(temporary) / 'config.yaml'
        config_path.write_text(yaml.safe_dump(config))
        env = {**os.environ, 'GATEWAY_CONFIG': str(config_path)}
        try:
            for name, module, port in [('providers', 'scripts.mock_multi_provider:app', mock_port), ('gateway-a', 'ai_gateway.main:app', first), ('gateway-b', 'ai_gateway.main:app', second)]:
                log = (output / (name + '.log')).open('w')
                logs.append(log)
                processes.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', module, '--host', '127.0.0.1', '--port', str(port), '--no-access-log'], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT))
            with httpx.Client(timeout=1, trust_env=False) as client:
                for port, path in [(mock_port, '/stats'), (first, '/health'), (second, '/health')]:
                    deadline = time.monotonic() + 20
                    while True:
                        try:
                            if client.get(f'http://127.0.0.1:{port}{path}').status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        if time.monotonic() > deadline or any(p.poll() is not None for p in processes):
                            raise RuntimeError(f'Service startup failed; inspect {output}')
                        time.sleep(0.1)
            result = subprocess.run([sys.executable, 'scripts/e2e_gateway.py', '--url', f'http://127.0.0.1:{first}', '--mock-url', f'http://127.0.0.1:{mock_port}', '--second-url', f'http://127.0.0.1:{second}'], cwd=root, capture_output=True, text=True)
            (output / 'acceptance.log').write_text(result.stdout + result.stderr)
            print(result.stdout, end='')
            if result.returncode:
                print(result.stderr, file=sys.stderr, end='')
                return result.returncode
            for log in logs:
                log.flush()
            # Lifecycle logs must never contain client or upstream credentials,
            # arbitrary upstream error text, or prompt contents.
            for name in ('gateway-a.log', 'gateway-b.log'):
                text = (output / name).read_text()
                for sentinel in ('gateway-demo-client-key-0123456789abcdef', 'upstream-openai-demo', 'upstream-secret-sentinel', 'FAIL_AFTER', 'SLOW_PREFETCH'):
                    assert sentinel not in text, (name, sentinel)
            print('PASS lifecycle logs omit credentials and prompt/error text')
            print('Evidence:', output)
            return 0
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for log in logs:
                log.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--redis-url', default='unix:///tmp/gw-integration-redis.sock')
    args = parser.parse_args()
    raise SystemExit(main(args.redis_url))
