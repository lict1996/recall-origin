# ADR-0004：双时态与单调账本序列

- 状态：Accepted
- 日期：2026-08-30
- 决策范围：RecallOrigin v1

## 背景

现实事件发生时间、系统摄取时间和系统改变认知的时间并不相同。仅使用 `created_at` 或单个 `as_of` 会让未来才摄取的内容泄漏进历史回答，也无法稳定处理相同时间戳和时钟漂移。

## 决策

1. Event 保存：
   - `event_time`：现实事件发生时间。
   - `recorded_at`：系统摄取时间。
   - `ledger_seq`：提交事务的单调序列。
2. Claim 保存 `valid_from/valid_to`，表示声明在现实世界中的有效区间。
3. Revision 保存 `revision_time/tx_from_seq`，表示系统何时形成当前认知。
4. 历史查询必须同时接受：
   - `valid_at`：询问现实世界的哪个时间点。
   - `known_at_seq`：只使用当时账本已经知道的内容。
5. current 查询等价于 `valid_at=now` 且 `known_at_seq=current ledger head`。
6. `tx_to_seq` 由下一 revision 推导；相同墙上时间的顺序以 ledger seq 为准。
7. 隐私 tombstone 对所有时间查询具有优先级。被删除的正文不会因为请求更早的 `known_at_seq` 而重新出现。

## 不变量

- revision 必须满足 `tx_from_seq <= known_at_seq`。
- claim 必须满足 `valid_from <= valid_at < valid_to`；开放上界使用 NULL。
- 较晚摄取、但自称很早发生的 event 不能进入较早 `known_at_seq` 的结果。
- evidence 已物理过期时，历史查询返回 `evidence_expired/insufficient`，不伪造完整回放。

## 影响

当前 FTS 路径可以先服务 current 查询；历史检索至少要有正确的 SQL resolver。后续若增加历史全文索引，仍必须以 canonical 双时态过滤为最终权威。
