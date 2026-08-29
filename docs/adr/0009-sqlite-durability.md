# ADR-0009：SQLite Durability Profile 与故障边界

- 状态：Accepted
- 日期：2026-08-30
- 决策范围：RecallOrigin v1 本地模式

## 背景

“事务成功”在进程崩溃、OS 崩溃和突然断电下不是同一个承诺。SQLite 的 WAL、synchronous、文件系统和硬件缓存共同决定持久性，不能用一次 SIGKILL 测试宣称覆盖断电。

## 决策

1. 所有连接启用 `foreign_keys=ON`、WAL 和有限 `busy_timeout`。
2. 默认 `durable` profile 使用 `synchronous=FULL`，并在平台支持时启用 fullfsync；写成功以权威账本事务 commit 为准。
3. 可选 `balanced` profile 使用 `synchronous=NORMAL`，明确允许断电时丢失最近已确认事务。
4. 写事务使用 `BEGIN IMMEDIATE`、保持短小；任何模型、网络或慢文件操作都不在事务内。
5. process crash、OS crash、power loss 分别测试和声明。Phase 1 的普通 subprocess fault test 只证明 process-crash 行为。
6. WAL snapshot 使用 SQLite backup API 或 `VACUUM INTO`，冻结相关 worker，并记录 schema/index/formation version、max ledger seq 和 purge registry head。
7. restore 前执行 integrity、migration 和 purge-registry freshness 检查；先应用 deletion ledger，再重建 current view 与派生索引。
8. `secure_delete`、WAL checkpoint/truncate 和 VACUUM 只描述引擎采取的措施，不承诺覆盖文件系统、SSD 或云快照。
9. SQLite v1 的目标是高可靠单机，不宣称分布式高可用。

## 不变量

- commit 前崩溃不得留下半条可见记忆。
- commit 后客户端未收到响应时，以原幂等键重试只产生一次逻辑效果。
- disk full、read-only 和 lock timeout 返回可判别错误，不返回假成功。
- 索引损坏不改变 canonical ledger；检索显式降级或失败，不把不完整结果冒充正常。

## 影响

发布资料必须把测试过的 profile、SQLite 版本、OS 和故障模型写清楚。更强的电源故障声明需要专用 VFS、虚拟机或硬件断电测试。
