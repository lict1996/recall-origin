# ADR-0002：Partition、Origin 与 Subject 分离

- 状态：Accepted
- 日期：2026-08-30
- 决策范围：RecallOrigin v1

## 背景

记忆在讲谁、由谁写入、从哪次会话产生，以及谁有权读取，是四个不同问题。把 session、agent、subject 或路径前缀当作授权 scope，容易造成错误共享或跨空间泄漏。

## 决策

1. `AuthorizationPartition` 是唯一的存储、授权和检索边界，由完整 tuple
   `(tenant_id, namespace_kind, namespace_id)` 精确确定。
2. v1 的 namespace kind 为 `workspace`、`user`、`agent_private`、`session_private`。
3. workspace 和 user 是默认长期 partition；agent/session 私有 partition 必须显式选择。
4. `OriginContext` 保存 `session_id`、`host_agent_id`、`producer_id` 和
   `request_id`，但不自动缩窄长期记忆的受众。
5. `SubjectRef` 只说明记忆在讲谁或什么，不能推导读取权限。
   subject 删除与永久写入 fence 依赖显式 lineage，并按
   `(partition_id, subject_id)` 匹配，不使用 `subject_type` 区分。
6. CLI 和展示层可继续使用易懂的 `scope` 字符串；服务层必须先把它规范化成一个或多个 exact partition。
7. 不支持隐式父级、路径前缀或“先全库召回再过滤”。每条查询、索引记录、cache key、job 和导出都携带 partition。
8. tenant 和 principal 来自受信任启动配置或认证 adapter；请求体只能缩小授权范围，不能自报身份或扩大范围。

## 不变量

- 带对象 ID 的 get、govern、feedback、forget、purge/status、formation job、
  retrieval trace、pack read 和 export 都先解析对象所属 tenant，再做 exact
  partition 与 capability 授权。跨 tenant 或不在 principal exact allowed
  partitions 中的对象一律返回 not-found，且不返回所属 partition 细节。
- export 在 same-tenant resolve 后同时要求 `read` 与 `export`。
- evidence、claim、relation 和 revision 之间使用带 `partition_id` 的复合外键，禁止跨 partition lineage。
- 相同 subject 不表示相同权限。
- subject 删除只覆盖显式 `claim_subjects`/`event_subjects` lineage；未标注、
  仅在正文提及该 subject 的内容无法自动发现或清除，未来未标注写入也不会被
  subject fence 阻止。
- partition/subject fence 在 alpha 中为永久 scope retirement。命中 fence 的
  persisted `remember`/`capture` 在消费 ledger/idempotency 或写正文前拒绝。
- UUID/ULID 的不可猜测性不构成授权。

## 影响

同一 workspace 中的 Codex、Claude Code 和其他 Agent 可以协作；需要隔离时必须建立私有 partition。调用方需要显式传 scope 和 subject lineage，但授权语义可测试且不会依赖模糊的命名约定。由于 alpha 没有 unretire，删除后恢复写入必须改用新的 partition 或 subject ID；这些标识也不应直接承载邮箱等 PII。
