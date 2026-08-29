# RecallOrigin

[![CI](https://github.com/lict1996/recall-origin/actions/workflows/ci.yml/badge.svg)](https://github.com/lict1996/recall-origin/actions/workflows/ci.yml)
[![CodeQL](https://github.com/lict1996/recall-origin/actions/workflows/codeql.yml/badge.svg)](https://github.com/lict1996/recall-origin/actions/workflows/codeql.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)](https://www.python.org/)
[![许可证：Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

**记忆要带凭证。**

RecallOrigin 是一个本地优先、面向 AI Agent 的记忆运行时。它把记忆保存为
“有证据关联、可修订、有明确作用域的声明”，并说明每条记忆为什么会被召回。

[English](README.md) ·
[架构](docs/concepts/architecture.md) ·
[安全](docs/security/threat-model.md) ·
[复现实验](REPRODUCING.md)

> [!IMPORTANT]
> RecallOrigin `0.1.0a0` 是早期 alpha，面向可信单机环境。它不是分布式高可用
> 服务、远程多租户平台，也不宣称达到了 SOTA 记忆效果。当前支持的拓扑是
> “每个 store 只由一个应用进程持有”。项目不内置模型总结器或抽取器；需要自动
> formation 时，由 Host 提供候选。

## 为什么做这个项目

很多 Agent 记忆 Demo 把“写进去”做得很简单，却没有回答这些问题：

- 谁可以看到这条记忆？
- 它当时成立吗、现在还成立吗、系统是什么时候知道的？
- 哪个事件能支持它？
- 它只是模型总结，还是已经被人确认？
- 为什么检索会选中它？
- 删除能否覆盖声明、索引、缓存包以及旧备份恢复？

RecallOrigin 把记忆视为 **claim（声明），而不是 truth（真相）**。SQLite 账本是
权威来源；FTS、可选向量结果、检索轨迹和 Evidence Pack 都只是派生视图，返回
前必须重新经过权威授权与删除检查。

| 核心问题 | RecallOrigin `0.1` 的处理方式 |
|---|---|
| 作用域 | 精确的 `workspace`、`user`、`agent_private`、`session_private` partition |
| 时间 | 现实有效时间 + 单调事务时间 |
| 来源 | 不可变 event、evidence link、claim 与 revision 链路 |
| 检索 | exact + FTS5，可选强制 partition 过滤的向量适配器，版本化 RRF |
| 注入 | 明确标为不可信数据，不进入更高优先级指令层 |
| 删除 | 立即 fence、引擎管理层 purge、带签名的防复活 registry |
| 运行 | 默认离线、无账号、无 API Key、无遥测 |

## 五分钟快速体验

使用 [`uv`](https://docs.astral.sh/uv/) 安装 GitHub prerelease 中的通用 wheel：

```bash
uv tool install \
  "https://github.com/lict1996/recall-origin/releases/download/v0.1.0a0/recall_origin-0.1.0a0-py3-none-any.whl"
```

如果希望在安装前校验 checksum 与 GitHub provenance，请按
[Release 产物验证步骤](REPRODUCING.md#verify-github-release-artifacts)执行。

创建独立演示库，记住一个事实，召回它，并导出文件化 Evidence Pack：

```bash
DEMO_DIR="$(mktemp -d)"
DEMO_DB="$DEMO_DIR/memory.sqlite3"

recallctl init --db "$DEMO_DB"
recallctl remember \
  "This workspace uses uv for Python dependencies." \
  --scope workspace:demo \
  --memory-key workspace.package_manager \
  --external-event-id quickstart-remember-001 \
  --idempotency-key quickstart-remember-001 \
  --db "$DEMO_DB"
recallctl search \
  "How are Python dependencies installed?" \
  --scope workspace:demo \
  --db "$DEMO_DB"
recallctl context \
  "Prepare to change Python dependencies" \
  --scope workspace:demo \
  --mode evidence \
  --ttl-seconds 600 \
  --out "$DEMO_DIR/evidence-pack" \
  --db "$DEMO_DB"
```

一次真实运行会召回这条声明，并生成：

```text
1. This workspace uses uv for Python dependencies. [mem_…]

evidence-pack/
├── MANIFEST.md
├── inspector.html
├── manifest.json
├── retrieval.json
├── memories/mem_….md
└── sources/evd_….json
```

显式导出的目录不再属于 RecallOrigin 的 managed purge 边界，CLI 会明确打印这条
警告。具体边界见[删除边界](docs/security/deletion-boundary.md)。

从源码开发：

```bash
git clone https://github.com/lict1996/recall-origin.git
cd recall-origin
uv sync --locked --all-extras
uv run recallctl doctor --json
```

## 可以真正检查的证据

Evidence Pack 是一份有预算的快照，不是一段隐藏提示词。manifest 列出被选中的
claim revision 与来源切片；`retrieval.json` 保存不含正文的排序轨迹；单文件
Inspector 完全离线、只读运行。

![RecallOrigin Inspector 展示召回分数与证据解释](docs/assets/inspector-retrieval.png)

引擎管理的副本带 TTL 和删除 lineage。资源采用私有文件权限、路径与体积检查，
导出 snapshot 时会依据 SHA-256 manifest 逐项校验，而 manifest 自身的摘要锚定在
数据库中。Inspector 使用默认拒绝的 CSP，并通过 `textContent` 渲染记忆内容。

## 选择最小的接入面

四种入口都调用同一个 application core，不在适配层重新实现授权、修订、检索或删除。

| 接口 | 适合场景 | 入口 |
|---|---|---|
| CLI | 人、Shell 自动化、CI | `recallctl` |
| Python | 内嵌 Agent、可测试工作流 | `MemoryEngine` |
| MCP stdio | Codex、Claude 和其他 MCP Host | `python -m recall_origin.interfaces.mcp` |
| Loopback HTTP | 本机进程边界 | `examples/http/serve_local.py` |

### Python

```python
from pathlib import Path

from recall_origin import (
    MemoryEngine,
    OriginContext,
    PartitionRef,
    RememberRequest,
    SearchRequest,
)

scope = PartitionRef.workspace("demo")

with MemoryEngine.local(Path("/absolute/path/to/memory.sqlite3")) as memory:
    memory.remember(
        RememberRequest(
            content="This workspace uses uv.",
            scope=scope,
            memory_key="workspace.package_manager",
            external_event_id="host-event-001",
            idempotency_key="host-event-001",
            origin=OriginContext(producer_id="my-agent"),
        )
    )
    hits = memory.search(SearchRequest(query="Python dependencies", scope=scope))
```

### MCP stdio

安装可选协议依赖：

```bash
python -m pip install \
  "recall-origin[mcp] @ https://github.com/lict1996/recall-origin/releases/download/v0.1.0a0/recall_origin-0.1.0a0-py3-none-any.whl"
```

Host 配置中固定绝对数据库路径与精确 partition：

```json
{
  "mcpServers": {
    "recall-origin": {
      "command": "/absolute/path/to/python",
      "args": [
        "-m",
        "recall_origin.interfaces.mcp",
        "--db",
        "/absolute/path/to/memory.sqlite3",
        "--tenant-id",
        "local",
        "--principal-id",
        "coding-agent",
        "--partition",
        "workspace:example-project"
      ]
    }
  }
}
```

默认只暴露 `memory_put`、`memory_search`、`memory_context`、`memory_get`、
`memory_feedback` 五个工具。只有 server operator 显式开启 capability 后才会出现
治理与删除工具。完整配置见 [MCP 示例](examples/mcp/README.md)和自动生成的
[协议契约](contracts/mcp-tools.json)。

#### 安全的 Agent 写入—召回闭环

Agent 显式调用 `memory_put(mode="remember", ...)` 后会得到 `claim_id` 和
`revision_id`，但新声明会有意保持为 `unverified` candidate，避免 Agent 静默确认
自己的总结：

1. 调用 `memory_search(..., include_candidates=true)` 检查 candidate。
2. 停止 MCP Host，使 store 继续满足“一库一应用进程”的支持边界。
3. 由可信本地用户检查正文和证据后执行：

   ```bash
   recallctl govern <claim_id> \
     --expected-revision-id <revision_id> \
     --action confirm \
     --reason "已依据引用来源完成人工复核。" \
     --db /absolute/path/to/memory.sqlite3
   ```

4. 重启 Host。普通 `memory_search` 和 `memory_context` 此时会返回 active、
   `user_confirmed` revision。

proposal 有 `memory_key` 时，确认和任何同 key supersession 会在同一个治理事务中
完成。在此之前，Agent、Service 写入和被 Policy Gate 保留的 model-derived
formation 输出只能创建 `unverified` candidate，或 reinforce 完整 claim identity
一致的现有 `candidate/unverified` head；该 identity 包括精确 partition、
`memory_key`、normalized content、kind、subtype、`valid_from` 和 `valid_to`。
这些路径不能继承、覆盖或 supersede `active/user_confirmed` claim。

如果可信本地用户直接录入事实，`recallctl remember` 会一步创建 active revision。
完整的 release 安装、Host 配置和生命周期见 [MCP 接入指南](examples/mcp/README.md)。

### Loopback HTTP

HTTP adapter 是固定 principal、只允许本机回环访问的源码示例，不会作为包命令安装：

```bash
git clone --branch v0.1.0a0 --depth 1 https://github.com/lict1996/recall-origin.git
cd recall-origin
uv sync --locked --extra server
uv run --extra server python examples/http/serve_local.py \
  --db /absolute/path/to/memory.sqlite3 \
  --scope workspace:example-project
```

它没有远程认证。不要绑定 `0.0.0.0`，也不要放在网络反向代理后。详见
[HTTP 示例](examples/http/README.md)和 [OpenAPI 3.1 契约](contracts/openapi.yaml)。

## 架构

![RecallOrigin evidence-first 架构](docs/assets/architecture.svg)

核心分层如下：

1. Capture 在同一事务提交 event 与 transactional outbox。
2. Formation 通过有 lease、可重试的 job 产生不可信 candidate。
3. claim、revision、evidence link 保存在权威 SQLite 账本中。
4. exact、FTS 与可选向量候选通过版本化 RRF 融合。
5. 返回 Fast/Evidence Context Pack 前重新执行 canonical hydration。
6. 删除 fence 的优先级高于 reader、worker、索引和旧备份。

进一步阅读[架构](docs/concepts/architecture.md)、[数据模型](docs/concepts/data-model.md)
和已经接受的 [ADR](docs/adr/)。

## 记忆形成与可信状态

- 本地 human 显式保存 semantic 或 episodic claim 时，会形成 active、
  `user_confirmed` revision。
- procedural memory 默认仍是 candidate，必须经过治理才能激活。
- Agent 与 Service 写入保持为 `unverified` candidate；被 Policy Gate 保留的
  model-derived 输出也只能 reinforce 完整 identity 匹配的
  `candidate/unverified` head，不能继承或压掉可信状态。
- 只有产生 `active/user_confirmed` 的 Human 写入，或 Human 执行
  `govern(confirm)`，才能 supersede 同一 `memory_key` 下的其他 live claim；
  `govern(confirm)` 的确认与 supersession 在同一事务提交。
- `capture` 只保证 event 和 outbox job 已持久化，不代表提取已经成功。
- 内置 `StructuredEventProvider` 用于接收 Host 已经抽取好的严格结构化 candidate；
  当前 alpha 不内置模型 extractor。
- instruction-like 自动 candidate 会进入 quarantine；即使已经激活，召回内容仍是
  不可信数据。

## 检索语义

检索始终在一个已授权的精确 partition 内进行，依次结合：

1. 稳定 key / exact match；
2. SQLite FTS5 lexical candidate；
3. 只有能强制 exact partition filter 时才启用的可选 vector adapter；
4. 确定性、带版本的 reciprocal-rank fusion；
5. partition、revision、valid time、evidence availability 与 deletion fence 的
   canonical 检查。

原始 FTS/vector 分数、RRF 分数、最终排序分数、选中 ID、配置和降级原因各自保留。
排序分数不是“这条声明为真的概率”。

## 删除合同

RecallOrigin 把删除明确拆成三层：

1. **逻辑不可见**：fence 提交后，所有引擎读取路径立刻看不到目标。
2. **引擎管理层 purge**：清理 canonical text、FTS、job、trace 和 managed
   Evidence Pack，并逐层报告状态。
3. **外部副本**：显式导出、文件系统/云快照、离线备份和远程 Provider retention
   不在引擎直接控制范围内。

当前 alpha 中，`forget` 只完成第一层；引擎管理范围内的物理清理需要 operator
显式调用 Python purge 或 `recallctl purge`，MCP/HTTP 不暴露该 purge 操作。不同
target 的语义也不同：purge claim 不会抹掉支持它的 event/evidence body；多来源
claim 删除一条 event 后，只要仍有其他 live evidence，就可能继续保留原 claim
text。

主库旁维护一份带签名的 purge registry，它不会随着旧数据库快照一起回滚。恢复时
若 registry 缺失、过旧或验签失败，引擎会拒绝恢复，而不是悄悄让已删内容复活。

Purge 会删除引擎管理的正文，但会有意保留部分可关联的审计元数据，包括调用方提供的
ID、`memory_key`、`subject_id` 和 revision reason。不要在这些字段里放需要彻底
擦除的正文、直接 PII 或邮箱地址。精确范围见[删除与残留元数据
矩阵](docs/security/deletion-boundary.md)。

## 可复现证据，而不是排行榜口号

仓库包含两类不同的证据：

- 覆盖幂等、CAS、worker lease 接管、删除竞态、异常关闭、只读错误、迁移和旧备份
  防复活的可靠性门禁；
- 支持 `100`、`10k`、`100k`、`1M` 文档的确定性离线检索 benchmark。

小语料只是一项 synthetic smoke test，不能证明广泛的现实问答质量。未配置 vector
adapter 时，vector baseline 会明确记录为 `null`，不会用估算值代替。

仓内 10k durable run 是在 macOS arm64 上进行的一次串行实测：

![RecallOrigin 10k durable synthetic benchmark](docs/benchmarks/results/scale-10000-durable-summary.svg)

| 10,000 条 synthetic document 的实测项 | 结果 |
|---|---:|
| 必过行为用例 | 4/4 |
| Recall@10 / MRR | 1.000 / 1.000（250 queries） |
| Search p50 / p95 / p99 | 133.909 / 226.763 / 239.992 ms |
| Durable 端到端写入 | 23.437 writes/s |
| SQLite 占用 | 41,177,088 bytes |
| 整条命令 wall clock | 465.59 s |

这些 unique-marker query 衡量确定性检索正确性与回归行为，不代表生产语义相关性。
这不是跨系统对比，没有 vector arm，也不能据此宣称 latency SLO。canonical artifact
SHA-256 为
`a8483be63b42dd93422ba74d4c3bf07cd6bb5a8f55c9c79ed9480826ded96387`。

详见 [benchmark 方法](docs/benchmarks/README.md)、
[完整实测 artifact](docs/benchmarks/results/README.md)与
[复现说明](REPRODUCING.md)。README 中的数字来自仓内 artifact，不使用估算值。

## 开发与验证

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src scripts
uv run --extra mcp python scripts/generate_mcp_contract.py --check
uv run python scripts/reliability_gate.py
```

Release 构建和 README quickstart 还会在干净虚拟环境中执行。贡献前请阅读
[CONTRIBUTING.md](CONTRIBUTING.md)、[SECURITY.md](SECURITY.md)和
[REPRODUCING.md](REPRODUCING.md)。

## 当前限制

- SQLite 是高可靠单机核心，不是分布式 HA。
- 一个 store 及其 managed pack root 只能由一个应用进程持有；不要让独立的 MCP、
  HTTP、Python 或 worker 进程同时指向同一 store。
- Loopback HTTP 尚无远程认证或企业多租户能力。
- FTS5 是零配置基线，不默认安装具体 vector 实现。
- 不包含托管同步、Web 控制面、图数据库或遥测。
- 显式导出的 Evidence Pack 和远程 Provider 副本无法由引擎直接清理。
- 物理 purge 和自动 formation processing 需要 operator 显式调用，不包含
  always-on supervisor。
- 现有 benchmark 不能替代 LongMemEval-V2、LoCoMo 或独立下游复现。

完整清单见 [docs/limitations.md](docs/limitations.md)，后续规划见
[docs/roadmap.md](docs/roadmap.md)。

## 参与贡献与许可证

带失败 fixture、威胁模型、adapter contract 或可复现 benchmark 的 Issue 尤其有价值。
提交 PR 前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题请按
[SECURITY.md](SECURITY.md)私下报告，不要创建公开 Issue。

RecallOrigin 使用 [Apache License 2.0](LICENSE)。
