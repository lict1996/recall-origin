# ADR-0008：删除、Purge 与旧备份恢复

- 状态：Accepted
- 日期：2026-08-30
- 决策范围：RecallOrigin v1

## 背景

删除 raw event 不会自动删除从它形成的长期记忆。FTS、cache、pack、trace 和旧 snapshot 也可能保留内容。单一“已删除”状态无法诚实表达引擎能控制和不能控制的副本。

## 决策

1. 删除合同分三层：
   - Logical invisibility：tombstone/fence 提交后，所有引擎读取立即返回 0。
   - Engine-managed purge：逐层清理 canonical content、FTS、outbox、managed cache/pack 和 trace。
   - External copies：用户导出、OS/云快照、离线备份和远程 Provider retention 单独列为 external/unverified。
2. typed target 支持 event、claim、subject、partition 和 managed pack。
   partition target 的 `target_id` 必须与显式 scope 完全一致；
   `expected_revision_id` 只允许用于 claim target。
3. `forget` 同步提交逻辑删除；physical purge 是单独的 Python/CLI
   操作。`cascade_policy` 只落库，alpha 没有异步 purge worker。
4. claim 删除若带 `expected_revision_id`，主库从最终 CAS 检查开始持有
   `BEGIN IMMEDIATE` 写锁，跨越 sidecar append 直到 tombstone commit。并发
   govern 只能在 CAS 前胜出，或在删除提交后得到 revision conflict。
5. partition/subject fence 在 alpha 中是永久 scope retirement，没有 unretire。
   persisted `remember`/`capture` 在同一事务、replay/ledger/idempotency/body
   write 之前检查 fence，命中返回 `SCOPE_DENIED`；`capture(persist=false)` 仍为
   `no_store`。subject 按 `(partition_id, subject_id)` 匹配，不区分
   `subject_type`，且只覆盖显式 `claim_subjects`/`event_subjects` lineage；
   未标注正文提及无法发现或清理，未来未标注写入也不会被阻止。
   partition 删除递增 `cancellation_epoch`；event、subject 和 partition 删除会
   取消相应 formation job。claim 和 subject 变为 tombstoned；event 删除在仍有
   live evidence 时保留原 claim 正文并追加绑定剩余 evidence 的 revision，最后
   一份 evidence 消失时变为 `unsupported`。claim/partition/subject 删除后的
   govern、feedback 和受影响 retrieval trace 返回 not-found；event 删除后若
   claim 仍有 live evidence，则不退休该 claim。alpha 不自动重新 formation。
6. purge 可以删除 event payload、claim content、evidence body 和 formation/job
   中的正文，这是不可变账本的隐私例外；claim purge 不删除支持它的
   event/evidence body。审计与幂等层会保留并非全部 opaque 的 metadata，包括
   event external/idempotency/session/host/producer/request ID、`memory_key`、
   `subject_id`、revision actor ID 与 `memory_revisions.reason`、deletion
   idempotency/target，以及 sidecar 中的 plaintext `target_id`。这些字段不得
   承载需要擦除的正文或 PII，`subject_id` 尤其不能直接使用邮箱。
7. 主数据库之外维护单调 purge registry。删除在主库逻辑提交前 durable append
   registry；中间崩溃时启动恢复以 registry 为准，允许保守多删。
8. 恢复旧主库时必须配合当前 registry 与 key。初始化校验 database identity、
   migration、SQLite integrity、HMAC chain、generation 和 checkpoint，再把
   sidecar fence 合并进主库；registry 缺失、过旧、错配或验签失败时拒绝打开。
   alpha 没有自动生成 snapshot manifest 或端到端 backup/restore 命令。
   若 authenticated sidecar tombstone 对应的 `deletion_requests` 缺失且
   partition 存在，`apply_fences` 在同一个主库 `BEGIN IMMEDIATE` 事务内幂等
   重建最小 lifecycle：保留 deletion ID、target、partition 和 registry
   timestamp，生成 recovery idempotency/request hash，使用
   `cascade_policy=safe`，将 logical visibility 标为 completed、其余 layers
   标为 accepted。已有 lifecycle 不被覆盖，原 deletion ID 可继续 status/purge。
   recovery 不伪造未提交的 tombstone revision 或 job status，read/worker/mutation
   由 fence fail closed，physical purge 仍需显式调用。同 typed target 的后续
   `forget` 复用该 recovery lifecycle 与原 deletion ID，不创建第二份 backlog。
   若旧 snapshot 早于该 partition，则只保留 authoritative fence，不创建本地
   lifecycle rows。
9. 恢复前快照中的 managed pack 不能仅凭旧 `status='active'` 读取；读取路径会把
   pack 的 claim、evidence/event、subject、partition 和 pack lineage 与合并后的
   fence 匹配。包含删除目标的旧 pack 继续不可读，不含删除 lineage 的后建安全
   pack 可以读取。claim matching、reinforcement 和 supersession 同样排除 current
   或 restored fence 覆盖的 lineage。
10. pack 写入采用 staging + atomic rename + SQLite registration。初始化只清理
    超过五分钟、名称可识别且未注册的 managed-root 直系 pack/staging 目录；
    registered、recent、unknown、symlink 和越界路径不删除。读取与删除已注册 pack
    时，stored root 必须仍是当前 configured `managed_pack_root` 的直系子目录；
    修改 root 配置不会自动迁移旧 pack，旧路径读取为 not-found、删除 fail closed，
    直到 operator 恢复原配置或显式迁移。

## 不变量

- tombstone 生效后，replay、reindex、worker commit 和 snapshot restore 都不能复活目标。
- partition/subject scope retirement 生效后，persisted write 不能消费新的
  ledger/idempotency 或留下正文；claim/partition/subject fence 后不得继续写
  governance、feedback 或暴露受影响 trace。
- FTS 命中必须经过 canonical hydration；未清理的旧索引行不能泄漏正文。
- 旧 snapshot 中的 managed pack 必须再次服从当前 sidecar fence。
- 每个 managed layer 有独立状态和错误，不把部分 purge 报成 complete。
- external/unverified 永远不计入“引擎管理层删除 100%”。
- retained identifier/reason metadata 不得承载调用方要求擦除的正文或直接 PII。

## 影响

purge registry 增加了一个必须备份和保护的 sidecar。claim 删除为保证 CAS 与
fence 顺序，会延长一次主库写锁。sidecar-first crash recovery 无法还原从未提交
的原始 request metadata，只能使用 recovery-derived metadata 与保守的 `safe`
policy，但同 target retry 会采用 recovered deletion ID。永久 scope retirement
意味着恢复写入必须换用新的 partition/subject ID。当前 metadata retention 仍有
linkability 与 reason 文本风险；后续方向是 domain-separated HMAC target handle
和可独立 purge 的 reason body。五分钟 crash-orphan grace 和保守路径筛选意味着
部分未注册明文可能暂时或永久留给 operator 处理。该恢复与 pack 生命周期合同只
支持单 application process；不声明独立 MCP/HTTP/worker 多进程协调。介质级删除
仍受 WAL、文件系统、SSD 和外部快照约束；需要更强保证时使用加密与
crypto-erasure。
