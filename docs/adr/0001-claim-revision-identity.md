# ADR-0001：Claim 与 Revision 的身份语义

- 状态：Accepted
- 日期：2026-08-30
- 决策范围：RecallOrigin v1

## 背景

Agent 生成的是可能被证据支持、冲突或推翻的声明，不是自动成立的事实。若把正文修改、确认状态、证据变化和 TTL 都做成同一行更新，就无法可靠解释历史，也无法安全处理并发治理和来源删除。

## 决策

1. `MemoryClaim` 表示正文、结构化载荷和现实有效区间固定的语义声明；公共 `claim_id` 同时是 `memory_id`。
2. 正文、含义或 `valid_from/valid_to` 改变时创建新 claim，不原地修改旧 claim。
3. `ClaimRevision` 只表达治理和证据状态，例如 candidate、active、conflicted、confirmation、TTL 和 evidence set。每次变化创建不可变 revision。
4. `tx_from_seq` 写入 revision；`tx_to_seq` 由下一条 revision 在 history view 中派生，不回写旧 revision。
5. `claim_heads` 是可重建的可变投影，以 compare-and-swap 更新 `current_revision_id`。
6. evidence set 绑定到 revision，而不是只绑定到 claim。
7. reinforcement 的完整匹配身份为 `(partition, memory_key, normalized_content,
   kind, subtype, valid_from, valid_to)`；`memory_key` 必须非空且所有字段相同，才可在
   同一 claim 上增加支持证据。Agent、Service 与 model-derived 输出还只能匹配当前为
   `candidate/unverified` 的 head，不能从 `active/user_confirmed` 继承状态。
8. 新 claim 可用 `supersedes` 或 `conflicts_with` 关系表达语义。只有 Human 写入
   `active/user_confirmed`，或 Human 执行 `govern(confirm)`，才能把同 key 的其他
   live claim 转为 `superseded`；后者的 confirm、CAS 与 supersession 在同一事务提交。
9. 所有公共读取都返回 `claim_id + revision_id`。

## 不变量

- 旧 claim、旧 revision 不因正常治理而被更新。
- revision 的 `previous_revision_id` 必须属于同一 claim，且前序 `tx_from_seq` 更小。
- 一个 claim 只有一个 current head。
- CAS 失败必须回滚新 revision，返回 `REVISION_CONFLICT`。
- 隐私 purge 是不可变性的显式例外：正文可以物理删除，但只留下不含可逆内容的删除回执。

## 影响

该模型比单表 memory 更复杂，但能支持可解释历史、确定性并发、来源删除和防复活。开放文本没有可靠 `memory_key` 时保持追加或冲突，不假装自动解决所有语义矛盾。
