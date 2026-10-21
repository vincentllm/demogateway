# 可观测性

可观测性模块提供独立的结构化 JSON 日志与独立的 Prometheus registry，生命周期钩子与路由层解耦。

## 职责

- 记录请求生命周期事件：`start` → `first_token` → `finish`。
- 输出 JSON 日志，**不包含** `Authorization`、API key、提示词、完整上游错误体。
- 维护 Prometheus metrics，标签仅使用静态白名单：`route`、`provider`、`outcome`。
- 在途请求计数正确增减，取消/失败/超时后归零。
- TTFT（Time To First Token）在首个包含 content/tool delta 的 chunk 到达时记录，不把注释/心跳当 token。

## 公共接口

```python
from ai_gateway.observability.lifecycle import Observability

obs = Observability(configured_routes={("chat", "openai")})

handle = obs.start(
    request_id="req-001",
    client_id="team-a",
    configured_route={"provider": "openai", "route": "chat"},
    streaming=True,
)

# 首个有效 token chunk 到达时调用
obs.first_token(handle)

# 请求结束时调用，保证幂等
obs.finish(handle, outcome="success", usage={
    "prompt_tokens": 10,
    "completion_tokens": 20,
    "total_tokens": 30,
})
```

## Metrics 白名单

| 指标 | 类型 | 标签 |
|---|---|---|
| `requests_total` | Counter | `route`, `provider`, `outcome` |
| `request_latency_seconds` | Histogram | `route`, `provider`, `outcome` |
| `time_to_first_token_seconds` | Histogram | `route`, `provider`, `outcome` |
| `inprogress_requests` | Gauge | `route`, `provider`, `outcome` |
| `token_usage_total` | Counter | `route`, `provider`, `outcome` |

**禁止作为标签的字段**：`request_id`、`client_id`、原始模型名、`user`、上游 key id 等可变/高基数字段。

## 日志脱敏

日志中记录的信息示例：

```json
{
  "event": "request_finished",
  "provider": "openai",
  "route": "chat",
  "outcome": "success",
  "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
}
```

不会记录：
- `Authorization` 头值；
- 任何 API key（客户端或上游）；
- 提示词内容；
- 完整上游错误响应体（只记录错误类型/状态码）。

## 测试

```bash
pytest -v tests/test_observability.py
```

关键场景：
- 日志 JSON 输出不含敏感信息。
- metrics 标签不包含 request_id/model/client_id。
- 取消/失败后在途计数归零。
- TTFT 只在 content/tool delta 时记录。
- `finish` 幂等。

## 限制

- 不依赖 Starlette middleware 捕获流结束；必须由集成层在 transport 终态回调中调用 `finish`。
- 每个实例默认持有独立 `CollectorRegistry`；`obs.render_metrics()` 返回 Prometheus text bytes，`obs.metrics_content_type` 提供 HTTP Content-Type。集成层负责暴露 `/metrics`。
- `configured_routes` 是可信配置中的 `(route, provider)` 集合。未配置或未知 pair 统一归为 `unknown`，请求值不会扩大标签集合。未知 outcome 归为 `error`。
- Gauge 和开始计数器使用 `outcome="in_progress"`；finish 的延迟、token 计数使用终态 outcome。TTFT 只记录 streaming 请求且不接受 finish 后的事件。
- `finish` 与 `first_token` 使用 handle 内锁；重复 finish 无副作用。跨 Observability 实例的 handle 抛出 ValueError。
- 日志只投影 canonical usage 的非负整数（排除 bool，最大 2**53-1），error 只保留整数 HTTP `status_code`、固定枚举 `type`、布尔 `retryable`；字符串、message、body、未知键都不会记录。
- 注入原有 inc/dec/observe recording registry 的用法继续可用，但这类 registry 不支持 render_metrics（抛出 TypeError）。
