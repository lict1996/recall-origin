# ADR-0006：Formation、候选与幂等

- 状态：Accepted
- 日期：2026-08-30
- 决策范围：RecallOrigin v1

## 背景

CLI 重试、webhook 重放、worker 接管和 Provider 重试都可能重复形成记忆。若显式
写入与自动形成使用含混或不同的 trust-transition 规则，也会形成不可审计的真值路径。

## 决策

1. `formation_mode=explicit` 是同步、确定性 formation，不调用自动 extractor；其
   初始 trust state 由受信任 principal 类型和 memory kind 决定。Human 的非
   procedural 写入可直接成为 `active/user_confirmed`，其他显式写入是
   `candidate/unverified`。
2. `formation_mode=automatic` 的 Provider 输出必须经过 Policy Gate，并始终作为
   unverified proposal 处理，不因 Provider、Service 或 model 身份获得确认。
3. `formation_mode=automatic` 只在接收事务中持久化 event 和 extraction outbox；后续形成是 eventual consistency。
4. 每个 candidate 使用稳定的 `candidate_fingerprint`；唯一键为 `(event_id, formation_version, candidate_fingerprint)`。
5. 自动形成记录 formation version、policy hash、prompt hash、model fingerprint、schema version 和 code version。
6. event 的外部幂等语义是 `(tenant, producer_id, external_event_id)`；存储使用 per-tenant、版本化 keyed-HMAC，而不是裸 SHA。
7. 相同幂等键和相同 request hash 返回原最小 receipt；相同键但 payload 不同返回 `IDEMPOTENCY_KEY_REUSED`。
8. 物理 purge 后保留不含正文的 HMAC tombstone，防止相同外部事件被重放复活。
9. Agent、Service 和被 Policy Gate 保留的 model-derived 输出只能创建 unverified
   candidate，或在 `(partition, memory_key, normalized_content, kind, subtype,
   valid_from, valid_to)` 全部相同且当前 head 为 `candidate/unverified` 时
   reinforce。它们不能匹配或继承 `active/user_confirmed`，也不能执行 trusted
   supersession；程序记忆永不自动 active。
10. automatic Provider 输出必须是 tuple，最多包含 32 个 schema-valid candidate，
    且序列化 JSON 总量不超过 1 MiB。超限或畸形输出在任何 formation run、
    derived candidate 或 claim 提交前统一记为 `PROVIDER_OUTPUT_INVALID`，进入有界
    retry/dead-letter 流程。

## 不变量

- 同一 event/candidate 重放不会产生第二个可见 claim/revision。
- explicit formation 不进入 automatic extraction 队列。
- Provider 输出上限在派生写入前执行，失败不得留下部分 formation 状态。
- 只有 Human 的 `active/user_confirmed` 写入或 Human `govern(confirm)` 能把同
  key 的其他 live claim 转为 `superseded`；`govern(confirm)` 的确认与 supersession
  在同一事务提交。
- HMAC 标识密钥与内容加密密钥分离；轮换期间保留识别历史重放所需的旧 key version。
- request receipt 不保存正文、query 或可逆标识。

## 影响

调用方应为机器重试提供稳定的 idempotency key。没有外部 ID 的交互式 CLI 可以生成新键并返回，但后续自动重试只有复用该键才具备幂等性。
