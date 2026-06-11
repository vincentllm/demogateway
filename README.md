# AI Gateway

![CI](https://github.com/vincentllm/demogateway/actions/workflows/ci.yml/badge.svg)

> [!WARNING]  
> **这是 Python 快速原型 / Demo 项目，不是生产级高性能网关。** 当前实现为快速验证架构（限流、后端健康、Provider 抽象、降级冗灾）并避免编译等待，因此选择 Python + FastAPI。若用于生产高吞吐场景，建议重写为 **Rust + Pingora** 或 Go。详见 [RFD-001](docs/rfd/rfd-001-technology-stack.md)。
>
> 另外，当前为早期原型（v0.1），限流状态与后端健康指标均保存在进程内存中。若部署多实例 Gateway，需额外接入 Redis 等共享存储。

一个轻量级 LLM Gateway 原型，对外暴露**单一 OpenAI 兼容端点**，内部完成限流、后端压力感知、多 provider 适配、降级与冗灾。

**本项目主要用途：**

1. **快速 Demo**：无需编译，启动即用，验证 Gateway 核心能力。
2. **架构验证**：限流 Facade、后端健康分级、Provider 抽象、降级策略均可独立扩展。
3. **本地开发**：配合自带 Mock Backend，零 API key 跑通完整链路。

你可以：

1. 启动 Gateway，让它把请求转发给真实 OpenAI / 自研 LLM 服务。
2. 启动本仓库自带的 **Mock LLM Backend**，零 API key 跑通完整链路。
3. 自己写客户端发送请求，Gateway 会把任意请求规范成 OpenAI 格式并转发。

---

## 目录

- [Production Note](#production-note)
- [Features](#features)
- [Quick Start](#quick-start)
- [Run with Mock Backend](#run-with-mock-backend)
- [Run with Real OpenAI](#run-with-real-openai)
- [Send Your Own Request](#send-your-own-request)
- [Configuration](#configuration)
- [Extending the Gateway](#extending-the-gateway)
- [Architecture & Decisions](#architecture--decisions)
- [Development](#development)
- [Roadmap](#roadmap)

---

## Production Note

**This is a Python prototype, not a production-grade high-performance gateway.**

- **Why Python?** Fast iteration, no compilation step, rich LLM ecosystem, and easy demo delivery.
- **Why not Python for production?** Python's GIL, higher memory footprint per connection, and event-loop throughput ceiling make it unsuitable for high-throughput, low-latency gateway workloads.
- **Recommended production stack:** **Rust + Pingora** (or Go) for memory safety, async I/O performance, and proxy-grade throughput.

This repository intentionally keeps the core abstractions (`Provider`, `RateLimitAlgorithm`, `CostFunction`) simple and language-agnostic so that a future Rust/Pingora rewrite can reuse the same design. See [RFD-001](docs/rfd/rfd-001-technology-stack.md) for the full decision record.

---

## Features

- **组合式限流**
  - Facade 模式组合多个限流策略。
  - 支持 `request` 级别与 `token` 粒度。
  - 第一版实现固定窗口，算法与粒度正交可扩展。
- **后端压力识别**
  - 滚动窗口采集延迟、错误率、并发数、连续失败次数。
  - 后端状态分级：`HEALTHY` / `DEGRADED` / `UNHEALTHY`。
  - 自动健康探测 `/health`。
- **OpenAI 兼容**
  - 单一入口 `POST /v1/chat/completions`。
  - 支持 streaming（SSE）与非 streaming。
  - Provider 抽象接口，可扩展 zai 等其他服务商。
- **降级与冗灾**
  - 跨后端实例重试，自动排除失败实例。
  - 全部后端失败时返回 OpenAI 兼容的 fallback 响应。
  - 可配置模型降级映射（如 `gpt-4` → `gpt-3.5-turbo`）。

---

## Quick Start

### 1. 安装依赖

```bash
git clone <repo-url>
cd ai-gateway
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

或使用 [uv](https://github.com/astral-sh/uv)：

```bash
uv sync --extra dev
source .venv/bin/activate
```

### 2. 验证测试

```bash
pytest -v
```

期望结果：`27 passed`

---

## Run with Mock Backend

不需要 API key，本仓库自带一个可编程的 Mock LLM Backend。

### 启动 Mock Backend

```bash
python scripts/mock_backend_server.py --port 9999
```

默认监听 `http://127.0.0.1:9999`，暴露 `POST /v1/chat/completions`。

### 启动 Gateway（指向 Mock Backend）

```bash
# 使用 config.mock.yaml
export GATEWAY_CONFIG=config.mock.yaml
uvicorn ai_gateway.main:app --host 0.0.0.0 --port 8000
```

### 发送请求

```bash
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer any-key" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

你会收到来自 Mock Backend 的 OpenAI 格式响应：

```json
{
  "id": "chatcmpl-mock-xxxx",
  "object": "chat.completion",
  "model": "gpt-4o-mini",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! This is a mock backend response."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": { ... }
}
```

### 一键端到端 Demo

如果想一次性跑通“Mock Backend → Gateway → Client”的完整链路，包括非流式、流式、限流：

```bash
python scripts/e2e_demo.py
```

这个脚本会自动：

1. 启动 Mock Backend（端口 9999）。
2. 启动 Gateway（端口 8000，使用 `config.mock.yaml`）。
3. 发送非 streaming 请求。
4. 发送 streaming（SSE）请求。
5. 发送限流 burst（`config.mock.yaml` 中 request limit = 5）。
6. 打印 Mock Backend 实际收到的请求内容。
7. 关闭两个进程。

### 验证 Gateway 是否转换/转发了格式

Gateway 本身对外暴露 OpenAI 格式，对 OpenAI-compatible 后端目前主要是透传。你可以通过 Mock Backend 的请求日志查看它最终收到了什么：

```bash
# Mock Backend 启动后访问
 curl http://127.0.0.1:9999/admin/request-log
```

你会看到：

```json
{
  "requests": [
    {
      "method": "POST",
      "url": "http://127.0.0.1:9999/v1/chat/completions",
      "body": {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "Hello!"}],
        "stream": false
      }
    }
  ]
}
```

这证明：

- 客户端请求被 Gateway 接收。
- Gateway 按 OpenAI provider 转发到了 `/v1/chat/completions`。
- 请求体（model、messages、stream 等）被透传给后端。

如果你接入的是 zai 等非 OpenAI provider，未来可以在 `ZaiProvider.transform_request()` 中看到这里发生了格式转换。

### 模拟后端故障

修改 `scripts/mock_backend_server.py` 中的 `SenderBehavior`：

```python
app.state.sender = MockLLMBackendSender(
    SenderBehavior(latency_ms=100, failure_rate=1.0, failure_mode="http_503")
)
```

重启 Mock Backend 后，Gateway 会触发重试与 fallback。

---

## Run with Real OpenAI

```bash
export OPENAI_API_KEY=sk-xxx
uvicorn ai_gateway.main:app --host 0.0.0.0 --port 8000
```

默认使用 `config.yaml`，后端指向 `https://api.openai.com/v1`。

---

## Send Your Own Request

Gateway 会把客户端发来的请求规范成 OpenAI 格式，再按 provider 转发给后端。当前只要后端是 OpenAI-compatible，请求几乎原样透传。

### Python 示例

```python
import httpx

response = httpx.post(
    "http://127.0.0.1:8000/v1/chat/completions",
    headers={"Authorization": "Bearer your-key"},
    json={
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "Hello!"}],
        "stream": False,
    },
    timeout=30,
)
print(response.json())
```

### Streaming 示例

```python
import json
import httpx

with httpx.stream(
    "POST",
    "http://127.0.0.1:8000/v1/chat/completions",
    headers={"Authorization": "Bearer your-key"},
    json={
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "Hello!"}],
        "stream": True,
    },
) as response:
    for line in response.iter_lines():
        if line.startswith("data: "):
            data = line[6:]
            if data == "[DONE]":
                break
            print(json.loads(data))
```

---

## Configuration

所有配置通过 YAML 文件管理，支持 `${VAR:default}` 环境变量注入。

### 关键配置项

```yaml
rate_limiter:
  policies:
    - name: request-limit
      enabled: true
      algorithm: fixed_window
      granularity: request
      window_seconds: 60
      limit: 100
    - name: token-limit
      enabled: true
      algorithm: fixed_window
      granularity: token
      window_seconds: 60
      limit: 10000

backends:
  - id: openai-primary
    provider: openai
    base_url: "https://api.openai.com/v1"
    api_key: "${OPENAI_API_KEY}"
    weight: 1
    models: ["gpt-4", "gpt-3.5-turbo"]

resilience:
  max_retries: 3
  retry_status_codes: [429, 502, 503, 504]
  timeout: 30
  fallback:
    enabled: true
    message: "Service temporarily unavailable."
    model_downgrade:
      gpt-4: gpt-3.5-turbo

health_check:
  interval_seconds: 10
  timeout_seconds: 3
  unhealthy_threshold: 2

load_metrics:
  latency_threshold_ms: 2000
  error_rate_threshold: 0.2
```

### 切换配置文件

```bash
export GATEWAY_CONFIG=/path/to/config.yaml
uvicorn ai_gateway.main:app
```

搜索路径：当前目录 `config.yaml` → `/etc/ai-gateway/config.yaml` → `GATEWAY_CONFIG` 环境变量。

---

## Extending the Gateway

### Switch Rate Limit Storage to Redis

By default rate limit counters are stored in memory. To share state across multiple gateway instances, switch to Redis:

```bash
pip install redis
```

Then update your config:

```yaml
rate_limiter:
  storage:
    type: redis
    redis:
      host: localhost
      port: 6379
      db: 0
      key_prefix: "ai_gateway:rl"
  policies:
    - name: request-limit
      algorithm: fixed_window
      granularity: request
      window_seconds: 60
      limit: 100
```

No code changes are required. The `FixedWindowAlgorithm` uses the same `RateLimitStorage` interface for both memory and Redis.

### Add a New Provider (e.g., zai)

1. Create `src/ai_gateway/providers/zai_provider.py` (already present as a placeholder):

```python
from ai_gateway.providers.base import Provider

class ZaiProvider(Provider):
    @property
    def name(self) -> str:
        return "zai"

    def supports(self, route: str, body: dict | None = None) -> bool:
        return route == "/v1/chat/completions"

    def transform_request(self, backend, route, body):
        url = backend.config.base_url.rstrip("/") + route
        headers = {}
        if backend.config.api_key:
            headers["authorization"] = f"Bearer {backend.config.api_key}"
        # TODO: map OpenAI body to zai format if needed
        return url, body, headers

    def transform_response(self, raw, request_body):
        # TODO: map zai response back to OpenAI-compatible format if needed
        ...
```

2. Register it in `src/ai_gateway/providers/__init__.py`:

```python
from ai_gateway.providers.zai_provider import ZaiProvider

BUILTIN_PROVIDERS: list[Provider] = [
    OpenAIProvider(),
    ZaiProvider(),
]
```

3. Use it in your config:

```yaml
backends:
  - id: zai-backend
    provider: zai
    base_url: "https://api.zai.example.com"
    api_key: "${ZAI_API_KEY}"
```

No changes to `main.py` or the router are required. See [RFD-007](docs/rfd/rfd-007-provider-abstraction.md) for the full design.

---

## Architecture & Decisions

详见 [`docs/RFD.md`](docs/RFD.md)，包含：

- 为什么选择 Python + FastAPI（以及为什么不是 Go）。
- 限流模块的 Facade + 策略组合设计。
- 固定窗口、内存存储、Dummy Tokenizer 的取舍。
- 后端健康分级与负载均衡策略。
- Provider 抽象与 OpenAI 透传策略。
- 降级、重试、fallback 的设计 tradeoff。
- 已知局限性与后续方向。

---

## Development

### 项目结构

```
ai-gateway/
├── config.yaml                 # 真实 OpenAI 配置
├── config.mock.yaml            # Mock Backend 配置
├── src/ai_gateway/
│   ├── main.py                 # FastAPI 入口
│   ├── config.py               # 配置加载
│   ├── api/router.py           # 对外端点
│   ├── ratelimit/              # 限流模块
│   ├── backend/                # 后端实例池与健康检查
│   ├── providers/              # Provider 抽象与实现
│   ├── resilience/             # 重试、fallback、熔断器
│   ├── tokenizer/              # Tokenizer 抽象与 Dummy 实现
│   └── testing/                # Mock Backend 工具
├── scripts/
│   ├── mock_backend_server.py  # 独立 Mock LLM 服务
│   └── send_mock_request.py    # 直接调用 Mock Sender
├── tests/                      # 测试
└── docs/
    ├── rfd/                    # 技术决策记录（RFD 目录）
    └── SUMMARY.md              # 项目总体说明
```

### 运行测试

```bash
pytest -v
```

### 启动 Mock Backend 并发送请求

```bash
# 终端 1
python scripts/mock_backend_server.py --port 9999

# 终端 2
export GATEWAY_CONFIG=config.mock.yaml
uvicorn ai_gateway.main:app --port 8000

# 终端 3
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer any-key" \
  -d '{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hi"}]}'
```

---

## Roadmap

- [ ] **生产化重写：Rust + Pingora（或 Go）高性能网关实现**
- [ ] Redis 分布式限流
- [ ] 滑动窗口 / 令牌桶限流算法
- [ ] tiktoken 真实 Token 计数
- [ ] 配置热重载
- [ ] 接入熔断器到路由
- [ ] 结构化日志、Tracing、Prometheus Metrics
- [ ] zai provider 完整实现
- [ ] 动态服务发现（Consul / etcd）
