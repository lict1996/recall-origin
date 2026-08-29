# 本地 HTTP 接入示例

这个 adapter 面向同一台机器上的 Agent。它没有 HTTP 身份认证：启动时固定一个
principal 和一组 exact partitions，网络层只接受 loopback/Unix socket 客户端。
不要把它绑定到 `0.0.0.0`，也不要放在可被远程访问的反向代理后面。

从仓库根目录安装锁定的 HTTP extra 并启动示例：

```bash
uv sync --locked --extra server
uv run --extra server python examples/http/serve_local.py \
  --db "$PWD/.local/recall-origin.sqlite3" \
  --scope workspace:demo
```

写入记忆（所有写接口都使用 `Idempotency-Key`；同一逻辑请求重试时复用同一个值）：

```bash
curl --fail-with-body http://127.0.0.1:8765/v1/memories \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-remember-0001' \
  -d '{
    "content": "发布前必须先跑完回归测试。",
    "scope": {
      "namespace_kind": "workspace",
      "namespace_id": "demo"
    },
    "kind": "procedural",
    "subtype": "workflow",
    "external_event_id": "example-event-0001",
    "origin": {
      "session_id": "demo-session",
      "host_agent_id": "example-agent"
    }
  }'
```

检索：

```bash
curl --fail-with-body http://127.0.0.1:8765/v1/search \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "发布 回归测试",
    "scope": {
      "namespace_kind": "workspace",
      "namespace_id": "demo"
    },
    "include_candidates": true
  }'
```

Agent 可以从 `http://127.0.0.1:8765/openapi.json` 读取运行时生成的 OpenAPI。
返回的记忆和 Context Pack 始终是 `untrusted_data: true`，调用方不得把召回正文当作
系统指令执行。HTTP 响应默认携带 `Cache-Control: no-store` 和 `X-Request-ID`。

当前 governance 的 HTTP 幂等重放缓存在单个服务进程内，因此示例固定
`workers=1`。remember 和 deletion 仍由 Engine 提供持久化幂等语义。需要远程部署、
多 worker 或跨进程 governance 幂等时，应先接入真正的认证 adapter 和持久化请求账本。
