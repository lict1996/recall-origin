# ADR-0003：SQLite 本地模式的 Principal 与 Capability

- 状态：Accepted
- 日期：2026-08-30
- 决策范围：RecallOrigin v1 本地模式

## 背景

SQLite 没有数据库级行安全。RecallOrigin 又需要区分普通 Agent 写入、人工治理和删除权限，因此必须明确本地模式能提供什么安全保证，不能把单机文件权限宣传成强多租户隔离。

## 决策

1. SQLite 模式定位为受信任单用户或单团队的高可靠本地运行时，默认固定单 tenant。
2. principal 类型为 `human`、`agent`、`service`；授权由受信任启动配置中的
   principal capabilities 与 exact allowed partitions 共同决定。
3. engine capability 为 `read`、`write`、`govern`、`delete`、`export`、
   `stats`、`admin`。MCP 额外把 `feedback` 作为工具暴露开关，映射到 engine
   的 append-only `write` 权限。
4. `recallctl init` 只初始化/验证数据库和 signed purge sidecar，不创建 persisted
   principal/grant。schema 虽有 `partition_grants`，alpha runtime 不读取它；
   调用方通过 trusted startup 配置 principal，但不能在普通业务请求体中自报身份。
5. 普通 MCP Agent 默认暴露 read、write、feedback；govern 和 delete 不进入默认
   工具集。`memory_search` 每次都会写新的 retrieval trace，因此
   `readOnlyHint=false`、`idempotentHint=false`。`memory_context` 也会写
   retrieval trace，Evidence mode 还会写 managed pack，因此
   `readOnlyHint=false`。
6. Agent 与 Service 的显式写入固定为 `agent_claim + unverified`；被 Policy Gate
   保留的 model-derived 输出同样只能创建 unverified proposal。它们只可 reinforce
   完整身份匹配且当前为 `candidate/unverified` 的 head，不能匹配 trusted head、
   继承确认状态、覆盖或 supersede `active/user_confirmed`，也不能签发
   `user_confirmed`、`source_verified` 或 `test_verified`。
7. 本地 CLI 的 local-human principal 表示“该 OS 账户被配置为 human”，不是对
   屏幕前真人的密码学证明。共享机器应使用独立 OS 权限或未来的服务端认证。
8. 未配置远程认证时，HTTP 层只允许 loopback/Unix socket，并拒绝绑定 `0.0.0.0`。

## 不变量

- tenant/principal 从受信任环境派生，业务 payload 中不存在可覆盖字段。
- formation worker 只有 `write` 且命中当前 tenant 的 exact allowed partition
  才能 lease；最终 commit 前再次验证。
- 只有 Human 产生的 `active/user_confirmed` 写入或 Human `govern(confirm)` 可执行
  same-key supersession；`govern(confirm)` 的 CAS、确认 revision 与 supersession
  必须在同一事务完成。
- unmanaged pack export 同时需要 `read` 与 `export`。
- HTTP `/v1/stats` 需要 `stats` 并只聚合 exact allowed partitions；CLI
  `doctor` 是本地 operator 诊断，不是 tenant-scoped 远程 API。
- opaque object ID 若属于其他 tenant 或不在 exact allowed partitions 中，
  统一返回不带 scope details 的 not-found；authorization failure 不通过计数、
  唯一键错误或错误类型差异泄漏其他 partition 的存在性。

## 影响

v1 不声称 SQLite 提供敌对租户隔离。真正的多租户服务需要 PostgreSQL RLS/default-deny、非 owner 应用角色和独立认证设计。
