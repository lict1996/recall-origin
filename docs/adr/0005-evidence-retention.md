# ADR-0005：Evidence 保留与可访问性

- 状态：Accepted
- 日期：2026-08-30
- 决策范围：RecallOrigin v1

## 背景

只保存模型总结会把推断伪装成事实；只给 raw event 设置固定 TTL，又可能让 active claim 失去最后来源。来源删除、外部链接失效和权限变化也必须传播到记忆状态。

## 决策

1. 每个普通检索可见的 claim revision 必须关联至少一份
   `availability='available'` 且仍有 body 的 evidence。
2. evidence set 通过 `revision_evidence` 绑定到具体 revision，并以排序后的
   evidence ID 计算 `evidence_set_hash`。授权仍以 evidence 所属的 exact
   partition 为边界；alpha 没有独立的逐 evidence ACL。
3. `source_count` 是当前 revision 中仍 available 且有 body 的来源数，不是
   原始写入数，也不是事实可信度。
4. 删除一个 event 时，引擎把其 evidence 标为 deleted，并同步追加一个只绑定
   剩余 live evidence 的 revision。仍有来源时保留原 claim 正文与原状态；最后
   一份来源消失时，新 revision 变为 `unsupported` 并退出普通检索。仍有 live
   evidence 的 claim 不因这次 event 删除而退休，后续仍可 govern/feedback。
5. alpha 不会根据剩余 evidence 自动重新运行 formation 或重写多来源 claim
   摘要；调用方不能把保留正文理解成已经重新推导。
6. `valid_to` 表示现实语义不再成立；Evidence Pack 和 retrieval trace 的
   `expires_at` 表示引擎停止提供相应派生资源，二者不能互换。
7. 目前没有 raw event/evidence 的通用 TTL worker、pin policy 或后台 retention
   scheduler；schema 中的保留字段不等于已实现自动清理。
8. 程序记忆不会因普通 Agent 写入自动成为 active；本地 human 的 procedural
   显式写入也保持 candidate，后续治理状态不等于事实证明。
9. event、evidence body 和 claim content 分离存储，以支持内容 purge 后保留
   ID、hash、availability 和删除回执等最小 lineage。这些 ID 不保证全部 opaque；
   privacy-relevant retained metadata 与使用约束见 ADR-0008。

## 不变量

- 没有 live evidence body 的 claim 不进入普通检索。
- 删除来源后，新 revision 的 evidence link 不得继续包含被删来源。
- claim 摘要不能跨越其 exact partition
  `(tenant_id, namespace_kind, namespace_id)` 授权边界。
- 保留多来源 claim 的旧正文必须明确视为 alpha 的当前行为，而不是自动重形成。

## 影响

该策略保证检索结果仍有可访问来源，但不保证保留正文只由剩余来源支持。需要更强
语义保证的调用方，应在来源删除后显式重新 formation 或治理；通用 retention
worker、逐 evidence ACL 和自动摘要重写属于后续工作。
