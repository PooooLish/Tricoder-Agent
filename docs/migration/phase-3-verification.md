# Hcode 迁移 Phase 3 验证记录

日期：2026-09-03

范围：token-aware Context Manager、完整任务/工具回合压缩，以及大型工具结果的
Session 隔离暂存、分段回读、审计与清理。本阶段未接入 MCP、Extension Host，
也未增加或安装第三方依赖。

## 实现结果

- `ContextManager` 同时接受 token 与字符预算。Provider 返回有效
  `input_tokens` 时，将输入/输出 usage 绑定到精确请求前缀；前缀失配、usage
  缺失或无有效输入用量时，回退到按 UTF-8 字节计算的保守估算。
- 固定 system、当前 task 和当前任务完整工具回合不会被拆开；旧历史按完整任务
  块从新到旧保留。孤立 tool call/result 会从发送视图移除，多工具调用由现有
  `ActionProtocol.complete_round()` 判定完整性。
- 压缩说明只存在于 Provider 请求视图，不写回 `SessionContext`，避免污染会话
  历史和下一轮 usage 前缀。旧 `compact_messages` 与
  `compact_session_messages` 保留为兼容代理，字符模式维持既有顺序。
- `ToolResultSpillStore` 只接受绝对受管根目录，以 Session ID 哈希形成隔离目录，
  以系统随机引用命名，并拒绝符号链接和 Windows reparse point/junction 路径。
- spill 使用 UTF-8 字节单项/Session 配额和不覆盖发布；碰撞不会覆盖已有文件。
  模型只收到有界预览、字节数、SHA-256 与不含本机路径的引用。
- `read_tool_result` 只按当前 Session 的 opaque reference 和字符 offset 分段回读，
  不能接受路径或跨 Session 引用。审计不记录正文和绝对路径。
- Runtime 将结果放在 Session 数据库状态根下的 `runtime/tool-results`，首次装配
  会话时清除上次进程遗留内容；`/clear` 销毁当前 Session 结果。即使 SQLite
  临时写入失败，清空意图和 dirty 状态仍保留，同时临时正文立即销毁。
- SQLite schema 仍只包含既有 Session/Memory 数据；契约测试直接检查数据库，
  确认 spill 正文和绝对路径均未进入持久化会话。

Context usage-anchor 思路参考并适配自 Hcode 基线
`hcode/conversation.py` 与 `hcode/context/manager.py`；TriCoder 的消息分组、预算
兼容层和 spill 安全实现均围绕现有 Provider/Protocol/Tool/Runtime 边界编写。
具体归属已记录在根 `NOTICE`。

## TDD 与回归场景

- Context：固定消息、多工具完整回合、孤立 call/result、旧任务淘汰、当前超大
  回合、usage 锚点命中/失配/缺失、字符兼容，以及 Agent 下一任务消费 usage。
- Spill：系统命名、Session 隔离、无效/重复调用标识、大小与总量上限、碰撞不
  覆盖、权限失败、符号链接/junction 拒绝、分段读取、清理范围与清理后复用。
- Tool/Runtime：call ID 传播、动态读取工具、spill 失败时有界降级、审计元数据、
  状态目录绑定、启动遗留清理、`/clear` 成功和持久化失败路径。
- Session：测试数据库 schema 与内容不含工具正文或 spill 绝对路径。
- 新契约均先观察预期 RED，再进行最小实现；全项目回归包含 Phase 1/2、权限、
  命令策略、变更账本、CLI/TUI 与 Eval 既有行为。

## 最新验证证据

环境：Windows，Python 3.11.6，项目现有 `.venv`。

| 检查 | 结果 | 覆盖范围 |
| --- | --- | --- |
| Phase 3 focused suite | exit 0；309 tests，OK | Context、spill、Agent、Tools、Runtime、Session、Protocol、Provider |
| `python -B -m unittest discover -s tests -q` | exit 0；606 tests，82.877s，OK；4 skipped | 全项目回归 |
| `python -B -m compileall -q src tests` | exit 0 | 源码与测试语法/字节码编译 |
| `python -B -m tricoder eval evals\smoke --dry-run --no-color` | exit 0；3/3 validated | Eval 定义与隔离装配 |
| `workspace.py doctor tricoder-cli` | exit 0；0 findings | 项目交接契约 |
| `git diff --check` | exit 0 | 当前差异空白与冲突标记 |

4 个跳过项为既有平台能力测试；本阶段没有把跳过项描述为已覆盖。

## 限制与剩余风险

- 配置项仍名为 `max_context_chars`；为保持兼容，它的同一数值暂时也作为 token
  上限。后续可增加独立 token 配置和模型窗口注册表，但不应在本阶段破坏 CLI。
- Provider usage 是请求级锚点，不是逐消息 tokenizer。若 Provider 不返回有效
  `input_tokens`，UTF-8 字节估算偏保守但不能声称与真实 tokenizer 精确一致。
- system、当前 task 或当前完整工具回合自身超限时会整体发送，以避免产生不可
  执行的半回合；因此该单次请求可能超过配置预算并由 Provider 拒绝。
- spill 内容受 2 MB 单项和 10 MB/Session 默认上限保护，但仍是本机明文临时
  文件；其机密性依赖状态目录的 OS 账户权限，本阶段不提供加密存储。
- 当前只验证 Windows/Python 3.11.6；Linux/macOS 链接权限、Python 3.12 和
  三家真实 Provider 的 usage 字段差异仍未在本阶段执行。
- Phase 4（Extension Host 与配置信任模型）尚未开始，必须再次获得用户明确批准。
