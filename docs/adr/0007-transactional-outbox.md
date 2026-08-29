# ADR-0007：事务型 Outbox 与 Worker Fencing

- 状态：Accepted
- 日期：2026-08-30
- 决策范围：RecallOrigin v1

## 背景

SQLite 事务不能覆盖远程 Provider。若先提交 event 再单独创建 job，会丢任务；若 worker 在写 claim 后、标记完成前崩溃，会重复形成；若 lease 过期，旧 worker 还可能覆盖新 worker 的结果。

## 决策

1. automatic event 与 outbox job 在同一 SQLite 事务提交。
2. job 使用稳定 `dedupe_key`；formation job 的语义唯一键为 `(event_id, job_type, formation_version)`。
3. job 状态至少包括 pending、leased、retry_wait、done、dead_letter、cancelled。
4. lease 时递增 `lease_generation`，它是 fencing token；完成事务必须同时匹配 job ID、lease owner 和 generation。
5. lease 事务保持短小。Provider、文件和网络操作全部在事务外执行。
6. worker 在外发前、最终提交前都检查 deletion fence 和 `cancellation_epoch`。
7. candidate/claim/evidence/revision 等派生写入与 job done 在同一事务提交；done CAS 失败则派生写入一起回滚。
8. retry 使用 attempt、available_at 和指数 backoff；超过阈值进入可观察 dead letter，不静默丢弃。
9. error、trace 和 DLQ 默认只保存 opaque ID、错误码、配置版本和 keyed hash，不复制正文。

## 不变量

- accepted automatic event 必然对应一个 durable job 或明确的 policy no-store 结果。
- lease 被接管后，旧 worker 无权完成或复活目标。
- delete 与 worker 并发时 tombstone 获胜。
- at-least-once 执行通过 candidate/job 幂等约束收敛为至多一次逻辑效果。

## 影响

调用 Provider 可能重复发生，但数据库可见结果不会重复。Provider 侧若支持幂等键，应传递 formation/job 的稳定标识以减少外部副作用。
