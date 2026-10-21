# 性能与水平扩展说明

## 测试环境与真实环境的区别

- **Mock 环境**：`scripts/mock_backend_server.py` 返回固定内容，延迟可配置，用于验证功能与本地压测。它不能代表真实 LLM 的 TTFT、吞吐或错误分布。
- **真实供应商**：OpenAI/Claude/DeepSeek 的延迟、速率限制、并发上限和计费模型差异很大。所有容量结论必须引用真实供应商实测数据，不能从 mock 环境外推。

## 关键性能指标

压测脚本 `scripts/load_test.py` 输出以下指标：

- **吞吐（req/s）**：完成的全部请求数（含错误）/ 总耗时；成功数由 JSON/SSE success 单独报告。
- **错误分类**：JSON 成功、SSE 成功、429 拒绝、流中失败、其他错误。
- **端到端延迟 p50/p95/p99**：从发出请求到收到完整响应的时间。
- **TTFT p50/p95/p99**：流式场景下从发送请求到首个有效 token chunk 的时间。

错误与超时不计入成功；流中失败单独统计，避免把不完整流当成成功。

## 共享状态：Redis

默认限流与配额使用进程内存存储，**不能跨副本共享**。多实例部署时需要显式配置共享状态。限流的 Redis 配置示例：

```yaml
rate_limiter:
  storage:
    type: redis
    redis:
      host: redis
      port: 6379

```

配额层提供 `RedisQuotaStore`，由集成层注入 `QuotaManager`；这里没有可直接复制的 `quota.store` YAML 开关。Redis 失败时应 **fail-closed**（拒绝新请求），避免配额超卖。参考 `tests/test_quota.py` 的存储故障测试；Redis ACL 行为需要部署环境单独验证。

## 连接池与背压建议

- 部署时检查共享 `httpx.AsyncClient` 的 `max_connections` 与 `max_keepalive_connections`。
- 每个上游后端都有独立连接预算；下游请求超过连接池容量时应排队或返回 503，而不是无限制创建连接。
- 流式请求保持上游连接直到客户端关闭或流结束；客户端断连必须立即取消上游请求，释放连接。

## 优雅排空建议

以下是部署要求，当前 `/health` 未提供排空状态信号：

- 收到 SIGTERM 后，应停止接受新请求，等待在途请求完成（设置 `timeout` 上限），再关闭 `httpx.AsyncClient`。
- 健康检查端点 `/health` 在排空期间可返回非 200，便于负载均衡器将流量切走。

## 扩容策略

1. **水平扩容**：网关实例无状态，通过 Redis 共享 quota/限流即可水平扩展。前置负载均衡器按 round-robin 或 least-connections 分发。
2. **垂直扩容**：提高单个实例的 CPU/内存可提高事件循环吞吐量，但受 GIL 与单进程 asyncio 限制。
3. **按瓶颈扩容**：
   - 若连接池耗尽 → 增加 `max_connections` 或实例数。
   - 若 Redis 延迟高 → 增加 Redis 分片或本地缓存（注意配额准确性）。
   - 若上游 429 增多 → 降低并发或增加后端实例/模型副本。

## 容量结论

所有容量数字（如 "支持 X req/s"）必须来自：

- `scripts/load_test.py` 在目标硬件与目标后端上的实测；
- 真实供应商 key 的渐进压测；
- 持续监控后的再评估。

不要从 mock 环境或单线程理论值推断生产容量。
