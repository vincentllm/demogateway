# AI Gateway 项目说明

## 1. 项目目标

构建一个 LLM Gateway 服务，统一对外暴露 OpenAI 兼容的 `/v1/chat/completions` 端点，内部完成：

1. **组合式限流**：支持 request / token 粒度，可叠加多种策略。
2. **后端压力识别**：基于延迟、错误率、健康探测对后端实例分级。
3. **OpenAI 格式请求代理**：支持 streaming 与非 streaming，后续可扩展 zai 等 provider。
4. **降级与冗灾**：跨实例重试、业务降级响应、模型降级 fallback。

## 2. 技术栈

- Python 3.11+
- FastAPI + Uvicorn
- httpx（异步 HTTP 客户端）
- PyYAML + Pydantic（配置与模型校验）
- pytest + respx（测试）

## 3. 项目结构

```
ai-gateway/
├── README.md                 # 快速开始
├── SUMMARY.md                # 本说明文档
├── config.yaml               # 运行时配置
├── pyproject.toml            # 依赖与 pytest 配置
├── src/ai_gateway/
│   ├── main.py               # FastAPI 入口，组装所有组件
│   ├── config.py             # YAML 配置加载与 Pydantic 模型
│   ├── api/
│   │   └── router.py         # 对外端点定义
│   ├── models/
│   │   └── openai.py         # OpenAI 请求/响应 Pydantic 模型
│   ├── providers/
│   │   ├── base.py           # Provider 抽象与注册表
│   │   ├── openai_provider.py# OpenAI provider 实现
│   │   └── zai_provider.py   # zai provider 占位
│   ├── ratelimit/
│   │   ├── facade.py         # 限流 Facade：组合多个策略
│   │   ├── policy.py         # 原子策略：算法 + 粒度 + key
│   │   ├── base.py           # 算法/粒度/key 抽象接口
│   │   ├── fixed_window.py   # 固定窗口算法
│   │   ├── request_cost.py   # request 粒度 cost
│   │   └── token_cost.py     # token 粒度 cost
│   ├── backend/
│   │   ├── pool.py           # 后端实例池与选择器
│   │   ├── health.py         # 定时健康检查
│   │   └── load_metrics.py   # 负载指标与状态分级
│   ├── resilience/
│   │   ├── retry.py          # 重试逻辑
│   │   ├── fallback.py       # 降级响应生成
│   │   └── circuit.py        # 简单熔断器
│   ├── tokenizer/
│   │   └── dummy.py          # Dummy tokenizer
│   └── testing/
│       ├── mock_http_sender.py # Mock LLM 后端 + MockGatewayClient
│       └── __init__.py
└── tests/
    ├── conftest.py
    ├── test_ratelimit.py
    ├── test_backend_health.py
    ├── test_openai_proxy.py
    ├── test_resilience.py
    ├── test_gateway_with_mock_backend.py
    └── test_mock_http_sender.py
```

## 4. 核心设计

### 4.1 限流模块

限流拆分为**算法**、**粒度**、**策略**、**Facade** 四层：

- **RateLimitAlgorithm**：定义 `acquire(key, cost, now)` 接口。
  - `FixedWindowAlgorithm`：内存固定窗口计数。
- **CostFunction**：定义 `calculate(request, body)` 接口。
  - `RequestCostFunction`：cost 恒为 1。
  - `TokenCostFunction`：cost 由 tokenizer 计算。
- **RateLimitPolicy**：封装 algorithm + cost_function + key_builder + limit + enabled。
- **RateLimiterFacade**：持有多个 Policy，AND 组合；任一拒绝即 429。

策略切换与叠加通过配置文件中的 `rate_limiter.policies` 列表完成。

### 4.2 后端压力识别

- **BackendInstance**：包装后端配置 + `RollingMetrics`。
- **RollingMetrics**：滚动窗口记录 latency、error、inflight、consecutive_failures。
- **BackendPool.select(model, excluded)**：按状态优先选择 HEALTHY > DEGRADED > UNHEALTHY，同状态按权重随机。
- **BackendHealthMonitor**：定时对后端 `/health` 探测，更新健康状态。

### 4.3 Provider 抽象

- `Provider` 接口：`supports()` / `transform_request()` / `transform_response()`。
- `OpenAIProvider`：代理 `/v1/chat/completions`，透传响应，支持 SSE streaming。
- `ZaiProvider`：占位实现，后续填入格式映射。
- `ProviderRegistry`：按 provider name 或 route 解析。

### 4.4 降级与冗灾

- **重试**：`with_retry()` 对配置状态码重试，每次排除已失败后端。
- **fallback**：所有后端不可用或全部失败后，返回 OpenAI 兼容的固定内容响应。
- **模型降级**：配置 `resilience.fallback.model_downgrade`，高端模型失败后尝试低端模型。
- **熔断器**：`CircuitBreaker` 已实现，当前未接入路由，可按需使用。

## 5. 对外端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `POST /v1/chat/completions` | OpenAI 兼容聊天接口 | 唯一业务入口 |
| `GET /health` | 健康检查 | |
| `GET /admin/backends` | 后端状态与负载指标 | |

## 6. 运行方式

```bash
cd ~/ai-gateway
source .venv/bin/activate
uvicorn ai_gateway.main:app --host 0.0.0.0 --port 8000
```

默认监听 `http://127.0.0.1:8000`。

### 6.1 环境变量

`config.yaml` 默认引用：

```bash
export OPENAI_API_KEY=sk-xxx
```

也可修改 `config.yaml` 指向 mock 后端或本地服务。

### 6.2 curl 示例

```bash
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer any-api-key" \
  -d '{
    "model": "gpt-3.5-turbo",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

## 7. 测试方式

```bash
cd ~/ai-gateway
source .venv/bin/activate
pytest -v
```

当前共 27 个测试，全部通过：

- `test_ratelimit.py`：固定窗口、request/token 粒度、Facade 组合。
- `test_backend_health.py`：后端选择、压力状态。
- `test_openai_proxy.py`：Gateway 代理 OpenAI 后端。
- `test_resilience.py`：failover、fallback。
- `test_gateway_with_mock_backend.py`：Gateway + MockLLMBackendSender 集成。
- `test_mock_http_sender.py`：Mock 后端自身行为验证。

## 8. 配置说明

关键配置项：

- `rate_limiter.policies`：限流策略列表，支持同时启用 request 和 token 策略。
- `tokenizer`：dummy tokenizer 参数（`tokens_per_message`、`overhead`）。
- `backends`：后端实例列表，含 provider、base_url、api_key、weight、models。
- `resilience`：重试次数、重试状态码、超时、fallback 消息、模型降级映射。
- `health_check`：健康探测间隔、超时、失败阈值。
- `load_metrics`：延迟阈值、错误率阈值、滚动窗口大小。

## 9. 后续扩展点

- **限流算法**：新增 `RateLimitAlgorithm` 实现（滑动窗口、令牌桶），配置中切换。
- **限流存储**：当前内存，后续可扩展 Redis 实现分布式共享。
- **Tokenizer**：当前 dummy，后续可接入 tiktoken 等真实实现。
- **Provider**：新增 provider 文件并注册到 `ProviderRegistry`。
- **服务发现**：当前静态 YAML，后续可扩展 Consul/etcd。
- **熔断器**：`CircuitBreaker` 已提供，可接入 per-backend 或 per-route。
