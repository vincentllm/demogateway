"""Real HTTP acceptance checks against running gateway and deterministic providers."""
import argparse
import asyncio
import json
import time

import httpx

TOKEN = 'gateway-demo-client-key-0123456789abcdef'
TOOLS = [{'type': 'function', 'function': {'name': 'weather', 'description': 'Weather', 'parameters': {'type': 'object', 'properties': {'city': {'type': 'string'}}}}}]


async def main(url, mock_url, second_url=None):
    checks = []
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        def record(name):
            checks.append(name)
            print('PASS', name, flush=True)

        async def call(model='openai-demo', stream=False, text='hello', key=TOKEN, extra=None, base=url):
            body = {'model': model, 'messages': [{'role': 'user', 'content': text}], 'max_tokens': 32, 'stream': stream}
            body.update(extra or {})
            return await client.post(base + '/v1/chat/completions', headers={'Authorization': 'Bearer ' + key}, json=body)

        assert (await client.get(url + '/health')).status_code == 200
        record('health')
        before = (await client.get(mock_url + '/stats')).json()
        assert (await call(key='invalid')).status_code == 401
        assert (await call(model='forbidden')).status_code == 403
        assert (await call(extra={'unsupported': 'secret-sentinel'})).status_code == 400
        assert (await client.get(mock_url + '/stats')).json() == before
        record('auth denial, model grants and validation prevent dispatch')
        settle_key = 'gateway-settle-client-key-0123456789abcdef'
        statuses = [(await call(key=settle_key)).status_code for _ in range(3)]
        assert statuses == [200, 200, 429], statuses
        record('actual usage settles reservation; lifetime quota blocks unfunded call')
        error_key = 'gateway-error-client-key-0123456789abcdef'
        assert (await call(key=error_key, text='FAIL_BEFORE')).status_code == 503
        assert (await call(key=error_key)).status_code == 429
        record('unknown upstream usage retains reservation')
        for provider in ('openai', 'deepseek', 'claude'):
            alias = provider + '-demo'
            for tool in (False, True):
                response = await call(alias, extra={'tools': TOOLS} if tool else None)
                assert response.status_code == 200, (alias, response.status_code, response.text)
                body = response.json()
                assert body['model'] == alias
                assert body['usage'] == {'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13}
                message = body['choices'][0]['message']
                if tool:
                    assert json.loads(message['tool_calls'][0]['function']['arguments']) == {'city': 'Paris'}
                else:
                    assert message['content'] == 'Hello fixture'
                record(alias + (' JSON tools' if tool else ' JSON text'))
                response = await call(alias, True, extra={'stream_options': {'include_usage': True}, **({'tools': TOOLS} if tool else {})})
                assert response.status_code == 200, response.text
                data = [line[6:] for line in response.text.splitlines() if line.startswith('data: ')]
                assert data.count('[DONE]') == 1 and data[-1] == '[DONE]', response.text
                chunks = [json.loads(item) for item in data[:-1]]
                assert not any('error' in item for item in chunks), chunks
                assert any(item.get('usage', {}).get('total_tokens') == 13 for item in chunks)
                assert all(item.get('model', alias) == alias for item in chunks)
                deltas = [choice['delta'] for item in chunks for choice in item.get('choices', [])]
                assert any('tool_calls' in d if tool else d.get('content') == 'Hello fixture' for d in deltas)
                if tool:
                    calls = {}
                    for delta in deltas:
                        for part in delta.get('tool_calls', []):
                            target = calls.setdefault(part['index'], {'id': '', 'name': '', 'arguments': ''})
                            target['id'] += part.get('id', '')
                            target['name'] += part.get('function', {}).get('name', '')
                            target['arguments'] += part.get('function', {}).get('arguments', '')
                    assert len(calls) == 1, calls
                    actual = calls[0]
                    assert actual['id'] == 'call_fixture' and actual['name'] == 'weather', actual
                    assert json.loads(actual['arguments']) == {'city': 'Paris'}, actual
                record(alias + (' SSE tools/usage/DONE' if tool else ' SSE text/usage/DONE'))
        response = await call(stream=True, text='FAIL_BEFORE')
        assert response.status_code == 503, response.text
        assert 'upstream-secret-sentinel' not in response.text
        record('upstream error before headers is safe HTTP 503')
        response = await call(stream=True, text='FAIL_AFTER')
        assert response.status_code == 200 and '"error"' in response.text and '[DONE]' not in response.text
        assert 'upstream-secret-sentinel' not in response.text
        record('upstream error after headers is SSE error without DONE')
        body = {'model': 'openai-demo', 'messages': [{'role': 'user', 'content': 'SLOW'}], 'stream': True, 'max_tokens': 32}
        started = time.monotonic()
        first_delta_seen = False
        cancel_key = 'gateway-cancel-client-key-0123456789abcdef'
        async with client.stream('POST', url + '/v1/chat/completions', headers={'Authorization': 'Bearer ' + cancel_key}, json=body) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if 'Hello fixture' in line:
                    first_delta_seen = True
                    assert time.monotonic() - started < 0.8
                    break
        assert first_delta_seen, 'No content delta received from slow stream'
        await asyncio.sleep(0.2)
        assert (await client.get(mock_url + '/stats')).json().get('active', 0) == 0
        assert (await call(key=cancel_key)).status_code == 429
        record('first delta arrives before terminal; disconnect closes upstream')
        body['messages'][0]['content'] = 'SLOW_PREFETCH'
        before_prefetch = (await client.get(mock_url + '/stats')).json()
        async def prefetch_request():
            return await client.post(url + '/v1/chat/completions', headers={'Authorization': 'Bearer ' + TOKEN}, json=body)
        pending = asyncio.create_task(prefetch_request())
        deadline = time.monotonic() + 2
        try:
            while True:
                active = (await client.get(mock_url + '/stats')).json()
                if active.get('active', 0) == 1 and active.get('openai', 0) == before_prefetch.get('openai', 0) + 1:
                    break
                assert time.monotonic() < deadline, ('prefetch never entered upstream', active)
                await asyncio.sleep(0.02)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await asyncio.sleep(0.2)
        after_prefetch = (await client.get(mock_url + '/stats')).json()
        assert after_prefetch.get('active', 0) == 0, after_prefetch
        assert after_prefetch.get('closed', 0) == before_prefetch.get('closed', 0) + 1, after_prefetch
        assert after_prefetch.get('completed', 0) == before_prefetch.get('completed', 0), after_prefetch
        record('disconnect during upstream prefetch closes upstream')
        bases = [url, second_url or url]
        responses = [await call(key='gateway-limited-client-key-0123456789abcdef', base=base) for base in bases]
        assert [r.status_code for r in responses] == [200, 429], [r.text for r in responses]
        record('RPM admission shared across replicas' if second_url else 'RPM admission rejects second call')
        metrics = await client.get(url + '/metrics')
        assert metrics.status_code == 200 and 'request_latency_seconds' in metrics.text
        gauge = [line for line in metrics.text.splitlines() if line.startswith('inprogress_requests{')]
        assert gauge and all(float(line.rsplit(' ', 1)[1]) == 0 for line in gauge), gauge
        for secret in (TOKEN, 'upstream-openai-demo', 'secret-sentinel'):
            assert secret not in metrics.text
        record('Prometheus export, zero active gauge and bounded safe labels')
    print(json.dumps({'passed': len(checks), 'checks': checks}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    parser.add_argument('--mock-url', default='http://127.0.0.1:9999')
    parser.add_argument('--second-url')
    args = parser.parse_args()
    asyncio.run(main(args.url, args.mock_url, args.second_url))
