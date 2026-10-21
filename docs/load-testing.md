# 压测

压测脚本 `scripts/load_test.py` 用于在本地或测试环境评估 AI Gateway 的并发能力与延迟分布。

## 能力范围

- 并发发送 OpenAI 风格请求到网关。
- 支持 JSON 完整响应、SSE 流、混合模式。
- 分类统计结果：JSON 成功、SSE 成功、429 拒绝、流中失败、其他错误。
- 计算吞吐（req/s）、错误数、latency p50/p95/p99、TTFT p50/p95/p99。
- 错误与超时不算成功。

## 运行方式

```bash
# 1. 启动 mock backend 与 gateway
python scripts/mock_backend_server.py --port 9999 &
GATEWAY_CONFIG=config.mock.yaml uvicorn ai_gateway.main:app --host 0.0.0.0 --port 8000 &

# 2. 压测
python scripts/load_test.py --concurrency 10 --total 100 --mode mixed

# JSON 输出
python scripts/load_test.py --concurrency 20 --total 500 --mode sse --output json

# CSV 输出
python scripts/load_test.py --concurrency 20 --total 500 --mode json --output csv
```

## 输出示例

```text
Load test results
========================================
Total requests:     100
Throughput:         48.23 req/s
JSON success:       25
SSE success:        25
429 rejected:       25
Stream errors:      0
Other errors:       25
Latency p50/p95/p99: 0.123s / 0.234s / 0.456s
TTFT    p50/p95/p99: 0.045s / 0.078s / 0.120s
```

## 结果解读

- **json_success / sse_success**：网关正常处理并返回完整响应的请求数。
- **rejected_429**：被限流/配额拒绝的请求，说明已达到配置上限。
- **stream_error**：流式响应未正常结束（未收到 `[DONE]`）或流中出现 error event。可能表示上游断流或网关处理异常。
- **other_error**：超时、连接错误、HTTP 5xx 等。
- **TTFT**：仅针对 SSE 成功请求计算，反映首个 token 到达延迟。

## 限制

- 默认使用 mock backend，不能代表真实 LLM 的延迟、吞吐或错误分布。
- `mixed` 按请求序号固定交替 JSON/SSE；奇数请求总量时 JSON 多一个。并发完成顺序仍取决于响应时间。
- `--concurrency`、`--total`、`--max-tokens` 必须为正整数；`--timeout` 必须为有限正数。
- 无 TTFT 样本时 text 显示 `N/A`，JSON 为 `null`，CSV 为空值。
- SSE 以完整事件分隔（空行）解析；心跳、role、空 delta、usage 不计 TTFT；非空 content 或工具 function name/arguments 计 TTFT。必须收到完整 `[DONE]` 事件才视为成功。HTTP 状态先于 SSE body 分类，流中断和 malformed SSE 计 stream_error。
- 脚本会尽快发送请求，无 RPS 上限；压测前需确认连接池与端口容量足够。

## 与扩容文档的关系

容量结论（如 "支持 X req/s"）必须引用本脚本在目标硬件与目标后端上的实测结果。具体扩容策略见 `docs/performance-and-scaling.md`。
