# TriCoder CLI —— 项目深度解析与面试指南

> 本文档用于：1) 快速建立对项目的完整理解；2) 秋招/面试中结构化地讲解项目。
> 维护约定：代码结构变化时请同步更新本文档。文中引用以**类名 / 函数名 / 模块路径**为主，
> 尽量不依赖具体行号，避免重构后失效。`src/tricoder/` 下每个模块都有中文 docstring，可交叉核对。

---

## 1. 项目是什么

**一句话**：TriCoder 是一个**本地运行、强调可控执行、会话记忆和多模型适配的 Coding Agent**，
它统一接入 OpenAI / DeepSeek / GLM 的原生结构化工具调用，并在**每次文件写入与命令执行前
要求人工审批**，全程写入可审计的 JSONL 轨迹。

**面试一句话讲法**：
> "我实现了一个类似 Claude Code / OpenCode 的本地编程 Agent，核心不是'让模型能干活'，
> 而是'让模型在严格边界内干活'——所有写操作和命令执行都经过策略校验 + 人工审批，
> 每一次运行都有脱敏审计轨迹，并且支持会话级记忆和多模型切换。"

**核心能力**（对应 README）：
- 统一 Provider 边界：三家模型响应归一化为内部 `ProviderResponse` / `ToolCall`
- 原生工具调用 + `legacy_json` 回滚协议
- 10 个工具：`list_files / read_file / search_text / glob_files / edit_file / create_file / apply_patch / run_command / git_diff / finish`（前 9 个为动作工具，`finish` 为任务收尾；`git_diff` 为只读 diff 预览）
- 受限 unified diff（`apply_patch`）：只允许修改/创建文件，不支持删除/重命名
- `--read-only`：禁止一切写入与命令执行
- 独立 Session 记忆（SQLite，只存结构化元数据，不存自由文本）
- 本地斜杠命令（`/session /model /status /clear /diff /undo /exit`）
- JSONL 脱敏审计
- Textual 交互 TUI

**技术栈与规模**：
- Python >= 3.11，依赖仅 `rich>=15,<16` 与 `textual>=8,<9`（均为纯 Python）
- 源码 `src/tricoder/` 约 6,300 行（17 个模块 + `tools/` 子包）
- 测试 `tests/` 406 项（`unittest`，无网络、无真实 API Key）
- 独立 Git 仓库（`projects/tricoder-cli`）

---

## 2. 架构地图

### 2.1 分层与依赖方向

```
                    ┌──────────────────────────────────────┐
   用户/终端 ──────► │  cli.py (run/chat/doctor/tui 入口)    │
                    └──────────────┬───────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
  shell.py (交互)            ui.py (rich/TUI)         session_runtime.py (装配/切换)
  tui.py (Textual)                                             │
                                                               ▼
                                       ┌──────────────────────────────────────┐
                                       │        agent.py (CodingAgent 循环)    │
                                       └──────────┬───────────────┬───────────┘
                                                  │               │
                                    ┌─────────────▼────┐   ┌──────▼──────────────┐
                                    │ protocols.py     │   │ providers.py        │
                                    │ ActionProtocol   │   │ ModelProvider 适配   │
                                    │ native/legacy    │   │ OpenAI/DeepSeek/GLM  │
                                    └──────────────────┘   └─────────────────────┘
                                                  │
                                    ┌─────────────▼──────────────────────────┐
                                    │  tools/ 工具包（ToolRegistry 聚合）      │
                                    │  binding/undo/handlers/search/write... │
                                    └─────────────┬──────────────────────────┘
                                                  │
                              ┌───────────────────┼───────────────────┐
                              ▼                   ▼                   ▼
                        policy.py            changes.py          audit.py
                       （路径/命令安全）     （变更账本/撤销）     （JSONL 脱敏）
                              │
                              ▼
                       sessions.py (SQLite 会话记忆)
```

依赖方向**单向向下**：`cli → session_runtime → agent → protocols/providers → tools → policy/changes/audit → sessions`。
`models.py`（数据模型）被所有层共享，是最底层依赖。

### 2.2 模块职责表

| 模块 | 职责 | 关键类/函数 |
| --- | --- | --- |
| `cli.py` | 命令行入口；`run / chat / doctor / tui` 装配 | `main`, `build_parser`, `_run_chat`, `_run_tui` |
| `models.py` | 跨模块数据模型 | `Message`, `ToolCall`, `ProviderResponse`, `RunResult`, `SessionContext` |
| `config.py` | 配置加载与优先级合并 | `load_config`, `preview_provider_models` |
| `agent.py` | Agent 主循环、上下文压缩、审计事件 | `CodingAgent`, `compact_messages`, `compact_session_messages` |
| `protocols.py` | 动作解析协议（native/legacy_json） | `ActionProtocol`, `NativeToolProtocol`, `LegacyJsonProtocol` |
| `providers.py` | 三家 OpenAI-compatible 服务适配 | `ModelProvider`, `OpenAICompatibleProvider`, `create_provider` |
| `tools/` | 工具包：注册表 + 各工具 + 安全发布基础设施 | `ToolRegistry`, `ToolContext`, `ToolHandler` |
| `tools/binding.py` | 目录绑定（POSIX dir_fd / Windows handle） | `_DirectoryBinding`, `_PosixDirectoryBinding`, `_WindowsDirectoryBinding` |
| `tools/undo.py` | 变更撤销与失败补偿 | `UndoExecutor` |
| `tools/handlers.py` | 工具处理器基类（快照/账本交互） | `ToolHandler` |
| `tools/{filesystem,search,write,command}.py` | 各工具实现 | `ListFilesTool`, `EditFileTool`, `ApplyPatchTool`, `RunCommandTool` 等 |
| `policy.py` | 工作区路径与命令执行安全策略 | `WorkspacePolicy`, `CommandPolicy` |
| `changes.py` | 任务内文件变更账本与 diff 渲染 | `ChangeJournal`, `render_change_set_diff` |
| `patches.py` | 受限 unified diff 的解析与应用 | `parse_unified_diff`, `apply_file_patch` |
| `audit.py` | 审计日志脱敏与 JSONL 持久化 | `AuditLogger`, `redact` |
| `sessions.py` | SQLite 会话存储与安全摘要 | `SessionStore`, `safe_requirement_summary` |
| `session_runtime.py` | 会话装配、原子切换、持久化 | `SessionRuntime` |
| `shell.py` | 交互 Shell（斜杠命令分发） | `InteractiveShell` |
| `ui.py` | Rich 终端 UI 与审批交互 | `TerminalUI` |
| `tui.py` | Textual TUI（模态审批） | `TricoderApp`, `ApprovalScreen` |
| `commands.py` | 斜杠命令纯解析 | `parse_command` |

### 2.3 三个扩展点（面试常问"怎么加新东西"）

1. **新增 Provider**：`config.py` 注册默认配置 + `providers.py` 注册工厂 + `cli.py` choices + `ui.py` label。约 5 个接触点（见缺陷 6.6）。
2. **新增工具**：在 `tools/` 下新建一个 `ToolHandler` 子类（声明 `name/description/parameters` + 实现 `run()`），并在 `tools/__init__.py` 的 `_HANDLER_CLASSES` 注册。
3. **新增动作协议**：在 `protocols.py` 实现 `ActionProtocol` 并注册，Agent 循环无需改动。

---

## 3. 核心数据流（面试必讲）

### 3.1 一次 `run` 任务的生命周期

1. `cli.main` 解析参数 → `load_config` 按"命令行 > 环境变量 > 项目 TOML > 默认值"合并，校验工作区、API Key、HTTPS base_url、协议。
2. 装配：`WorkspacePolicy` + `CommandPolicy` + `ToolRegistry` + `CodingAgent` + `AuditLogger`。
3. `CodingAgent.run` 进入轮次循环（默认最多 30 轮）：
   - 把消息历史按上下文预算压缩；
   - `provider.complete(messages, tools)` 请求模型；
   - `protocol.resolve_action(response)` 解析出**一个**工具动作或修正反馈；
   - 执行工具 → 得到 `ToolResult` → 构造 tool 消息回填 → 审计；
   - 若 `finish` 且"有修改则必须验证通过"→ 完成。
4. 退出码：`0` 完成；`1` 未完成/验证失败；`2` 配置或审计准备失败。

**关键点**：Agent 每轮**只接受一个结构化工具调用**；原生协议下如果模型返回 0 个或多个调用，
Agent 不执行任何工具，只回一条修正反馈（`NATIVE_TEXT_FEEDBACK` / `NATIVE_MULTIPLE_CALLS_FEEDBACK`）。

### 3.2 写操作 / 命令执行的审批链路（安全核心）

```
模型请求 edit_file/create_file/apply_patch/run_command
   │
   ▼
CommandPolicy / WorkspacePolicy 校验（路径越界？敏感路径？命令白名单？参数允许集？）
   │
   ▼
工具在“目录绑定”上读取真实快照（content/mode/inode identity）
   │
   ▼
生成 diff/命令详情 → 调用 approver(action, detail)  →  人工确认 y/yes
   │
   ▼
再次核验（TOCTOU：父目录身份 + 目标内容未变）
   │
   ▼
原子发布（写临时文件 → link/replace）→ 发布后核验 → 记录变更账本
```

**审批不是沙箱替代品**（README 明确），但它是"人做最终判断"的最后防线；策略层负责把
"值得审批的内容"缩到最小且把危险面挡住。

### 3.3 会话与记忆持久化

- 每个 Session 独立保存：名称、工作区绝对路径、Provider、模型、时间戳、修改文件路径、验证状态、受限长度的"安全摘要"。
- **关键设计**：SQLite 中**不保存任何自由文本**。任务原文只存 `原文未持久化（共 N 字符）`，
  运行结果只存固定格式 `run: succeeded/failed; modified_files=N; verification=...`。
- 完整消息上下文只在当前进程的 `SessionContext` 内存中；重启后只恢复结构化元数据。

### 3.4 撤销与变更账本

- `ChangeJournal` 在**单个任务内**记录每个路径的"最早前态 + 最新后态"（含 inode identity）。
- 非空任务封存为 `TaskChangeSet`；`/diff` 显示正向 diff，`/undo` 先预览反向 diff 再二次确认。
- 撤销是"全量核验 + 原子恢复 + 失败反向补偿"：撤销前重新检查每个目标的内容/mode/identity，
  任何外部冲突都**整体拒绝写入**。
- 账本仅存内存，进程重启即丢失（MVP 明确取舍）。

### 3.5 上下文预算与压缩

- `--max-context-chars` 控制发送给模型的字符预算（默认 80,000）。
- 压缩以**完整工具回合**为单位（assistant 动作 + 工具结果成对），避免截断半个回合。
- 固定保留前两条系统/任务消息 + 压缩提示；旧任务块整体丢弃。

---

## 4. 安全设计深度（项目最大亮点，面试重点）

### 4.1 分层防线

| 层 | 机制 | 防什么 |
| --- | --- | --- |
| 路径策略 | `WorkspacePolicy`：resolve + 敏感词 + 符号链接解析 | 目录穿越、`.git/.env/.ssh` 访问、链接逃逸 |
| 命令策略 | `CommandPolicy`：白名单 + 参数允许集 | 越界执行、外部代码加载、写 Git/依赖 |
| 审批 | `approver`：每次写/执行必须 y/yes | 模型"自主"做破坏性操作 |
| 审计 | JSONL 脱敏 + 不落自由文本 | 事后可追溯、不留敏感内容 |
| 只读模式 | `--read-only` 拒绝全部写入/执行 | 一次性分析场景 |

### 4.2 TOCTOU 防护：目录绑定（最容易讲深的技术点）

文件写入分"审批"与"发布"两步，中间存在**检查-使用竞态**（攻击/并发可在审批后替换目标）。
TriCoder 的做法是**把发布操作绑定到审批时确认过的父目录句柄**：

- **POSIX**：`open(parent, O_DIRECTORY|O_NOFOLLOW)` 拿到目录 fd，之后所有 `rename/link/unlink/open`
  都带 `dir_fd=` 相对于该句柄执行；`verify_parent()` 通过 `st_dev/st_ino` 确认目录未被换掉。
- **Windows**：用 `CreateFileW` 对父路径**每一级组件**持有目录句柄（`FILE_FLAG_BACKUP_SEMANTICS`，
  不共享 DELETE 权限），使目录在句柄存续期间无法被改名/删除；通过 `GetFileInformationByHandle`
  拿卷序列号 + file index 作为 identity。

**配套**：发布用"临时文件 → 硬链接/重命名"的**原子替换**；发布后再读一次快照校验
（内容/权限/identity），不一致则标记 `tainted`（该路径失去撤销所有权证明）。

> 面试讲法：先讲"审批和写入之间有竞态窗口"这个动机，再讲"目录句柄绑定 + 原子发布 + 发布后核验"。

### 4.3 命令策略的"可信可执行程序"

`CommandPolicy` 只允许三类命令：
1. `python -m {unittest, pytest, compileall, ruff, mypy}`（**禁止直接调用** pytest/ruff/mypy，
   防 Windows 从 cwd 命中同名恶意程序）；
2. `git {status,diff,show,log}` 只读查询（禁 `-C/-c/--git-dir/--work-tree/--no-index/--ext-diff/--paginate` 等）；
3. 每工具**允许参数白名单**（如 pytest 禁 `-p/--plugins/--pdb/--pyargs`，ruff 仅 `check` 且禁 `--fix`）。

校验后 `args[0]` 通过 `shutil.which` 解析为**可信绝对路径**，审批信息展示实际执行程序；
`subprocess` 始终 `shell=False`、`cwd` 限定在工作区内、有超时。

### 4.4 敏感信息最小化

- API Key 只进内存（`ProviderConfig`），不写 TOML/日志/审计；`.env.local` 由 gitignore 排除。
- 审计 `redact()`：递归替换 key 名含 `api_key/token/secret/...` 的字段为 `***`，
  自由文本字段（`content/output/patch`）替换为**字符数**，绝不落原文。
- SQLite 只存结构化元数据 + 长度占位（见 3.3）。

### 4.5 为什么安全设计"值得讲"

这套设计把"LLM 自主写文件"从"信任模型"变成"边界 + 审批 + 可审计"，是 Coding Agent 落地
最关键的工程问题。面试时讲清楚"威胁模型 → 分层防线 → TOCTOU 竞态 → 原子发布 → 最小化留存"
这一条线，比罗列功能更能体现深度。

---

## 5. 设计亮点清单（面试重点）

| # | 亮点 | 机制 | 怎么讲/延伸 |
| --- | --- | --- | --- |
| 1 | **Provider 归一化抽象** | `ProviderResponse/ToolCall/TokenUsage` 统一三家响应；`_PROVIDER_PROFILES` 按厂商档案封装差异（dialect、并行调用、thinking） | 对比"为每家写一套 if-else"；延伸：加新厂商只动适配层 |
| 2 | **协议策略模式** | `ActionProtocol` 把 native/legacy_json 的解析/反馈/回合判定收敛为策略对象；Agent 循环无协议分支 | 说明"循环与协议解耦"；延伸：加 anthropic `tool_use` 只需新实现 |
| 3 | **工具注册表 + 处理器基类** | `ToolRegistry` 按名分发；每个工具 = `ToolHandler` 子类（定义 + run）；共享快照/账本能力在基类 | 对比"一个巨型类写 8 个方法"；延伸：自定义工具/MCP |
| 4 | **目录绑定防 TOCTOU**（见 4.2） | dir_fd / Windows 句柄 + identity 核验 | 深度技术点，最能拉开差距 |
| 5 | **原子发布 + 失败补偿** | 临时文件 → link/replace；发布后核验；多文件补丁中途失败反向回滚 | 讲"要么全部成功要么安全回滚" |
| 6 | **变更账本与可撤销** | `ChangeJournal` 记录净变更；`/undo` 二次核验 + 反向补偿 | 讲"撤销也是安全操作，不盲目覆盖" |
| 7 | **审计脱敏与最小化留存** | JSONL 递归脱敏；SQLite 不落自由文本 | 讲"合规意识"；延伸：可审计性/合规审计 |
| 8 | **上下文预算按回合压缩** | 字符预算 + 完整工具回合为压缩单元 | 讲"避免截断半个工具回合导致上下文损坏" |
| 9 | **依赖注入的可测性** | `SessionRuntime` 7 个 factory 可注入；`JsonTransport`/`AgentObserver` 均为协议 | 406 项测试不联网、无真实 Key 全靠注入 |
| 10 | **双协议回滚** | 原生 tool calling 失败可用 `legacy_json` JSON 文本回滚 | 讲"对厂商协议差异的容错设计" |
| 11 | **只读模式** | 一键关闭全部写能力 | 讲"分析场景的降级路径" |
| 12 | **命令注册表** | `commands.py` 的 `COMMAND_SPECS` 集中命令元数据，两个 UI 的 `/help` 自动生成 | 讲"数据驱动的命令分发，加命令只改一处" |
| 13 | **Textual TUI** | 模态审批 + 线程安全事件桥接 | 讲"UI 与 Agent 循环线程模型" |

---

## 6. 已知缺陷与改进方向（面试也常问"你还知道哪些不足"）

按影响排序；每条给"问题 → 影响 → 方案 → 大致工作量"。

### 6.1 无 OS 级沙箱（高影响）
- **问题**：审批是"人的判断"，不是隔离。模型/被批准的命令仍在当前用户权限下运行；命令白名单内的
  pytest 可经 `conftest.py`/插件执行任意工作区代码。
- **方案**：把 `subprocess` 执行抽象为 `process_runner` 接口，后续接入 landlock（Linux）/ Job Object
  + Restricted Token（Windows）或容器作为可选纵深防御层；先收紧 pytest/ruff/mypy 参数集（已做一半）。
- **工作量**：大（跨平台、配置精细）。MVP 阶段定位为"审批+策略"主防线。

### 6.2 策略靠硬编码集合维护（中影响）
- **问题**：`CommandPolicy` 的允许参数集是代码内集合，工具新版本新增危险参数可能漏禁（黑名单思维残留）。
- **方案**：命令策略 schema 化/配置化（YAML/TOML 声明每工具的允许参数与值校验），或引入权威的
  参数解析器按 allow/deny 规则自动判定。
- **工作量**：中。

### 6.3 变更账本仅存内存（中影响）
- **问题**：进程重启后 `/diff`、`/undo` 历史消失。
- **方案**：把 `TaskChangeSet` 的**结构化元数据**（不含源码）持久化到 SQLite，撤销时重新读盘快照恢复。
- **工作量**：中。

### 6.4 检索性能与正则回溯（中影响）
- **问题**：`search_text` 是纯 Python 逐文件扫描（无 ripgrep）；正则回溯仅靠"长度/行长上限"近似防护，
  非理论保证。
- **方案**：集成 ripgrep（`--json`）作为可选后端，保留纯 Python 降级；正则可考虑 `regex` 模块的
  超时/`RE_TIMEOUT` 或改用带超时的子进程。
- **工作量**：中。

### 6.5 `.gitignore` 仅为常用子集（低-中影响）
- **问题**：`tools/gitignore.py` 只支持 basename/目录/`!` 取反/`**` 的近似语义，不完整兼容 Git。
- **方案**：要么引入成熟解析库，要么明确文档边界（已更新 README 如实说明"常用语义子集"）。
- **工作量**：低（文档已缓解）。

### 6.6 Provider 接入接触点多（低-中影响）
- **问题**：新增 Provider 要改 `config.py`、`providers.py`、`cli.py` choices、`ui.py` label 等约 5 处。
- **方案**：收敛为**单一注册表**（key_env/base_url/model/label/choices 一处声明），
  `cli`/`ui` 从注册表生成（阶段二计划）。
- **工作量**：中。

### 6.7 无流式输出（低影响）
- **问题**：Provider 档案声明了 `streaming`，但 `complete()` 是全量等待，无 token 流式。
- **方案**：扩展 `ModelProvider` 提供 `stream()`，`CodingAgent` 按块消费；TUI 可实时显示。
- **工作量**：中。

### 6.8 单进程单用户、SQLite 无迁移（低影响）
- **问题**：`SessionRuntime` 非线程安全、无并发会话写入策略；DB schema 无迁移版本。
- **方案**：明确单用户定位；为 `sessions` 表加 schema_version 与迁移钩子。
- **工作量**：低。

### 6.9 TUI 尚未完备（低影响）
- **问题**：`/session` 跨工作区切换、命令大输出分页、运行中退出时 worker 取消未实现。
- **方案**：见 `docs/framework/tui-framework.md` 的 Limitations。
- **工作量**：中。

---

## 7. 高频面试 Q&A（回答要点）

**Q1：为什么自己写一个 Coding Agent，不用现成开源（OpenCode/OpenHands/Aider）？**
> 参考它们的公开设计思想（有 `docs/open-source-assessment.md`），但按自己的目标取舍：
> 强调"可控 + 可审计 + 轻量"，主动放弃高级特性（检索、多 Agent、插件），换来极简依赖
> （只 rich + textual）、清晰的安全边界和可扩展架构。也体现"能看懂别人的设计并独立实现"。

**Q2：安全到底怎么保证的？**
> 四层：路径/命令策略 → 人工审批 → TOCTOU 目录绑定与原子发布 → 脱敏审计与最小化留存。
> 强调"审批不是沙箱替代品"，并说明威胁模型（见第 4 节）。

**Q3：多模型怎么统一？**
> 三层：`models.py` 定义厂商无关的 `ProviderResponse/ToolCall`；`providers.py` 的
> `OpenAICompatibleProvider` 负责请求序列化/响应解析/usage 方言归一；`_PROVIDER_PROFILES`
> 按厂商声明能力档案。加新模型 = 注册档案，不改 Agent。

**Q4：Agent 循环怎么设计？工具调用怎么调度？**
> 线性消息历史 + 轮次上限（默认 30）；原生协议下每轮可接受多个工具调用，但**顺序执行、每个动作独立审批与审计**（不并发，避免写操作竞态）；解析失败/无调用时回修正反馈不执行。
> 这是"可控优先于吞吐"的取舍：并行会破坏目录绑定 TOCTOU 防护与逐动作审批语义。

**Q5：怎么保证测试可重复、不花钱？**
> 依赖注入：`JsonTransport`/`AgentObserver`/7 个 factory 全部可 mock；测试用 FakeProvider/
> 临时工作区/临时 SQLite，406 项不联网不耗 token。安全工具测试通过注入故障 binding 模拟
> TOCTOU/发布失败/补偿失败等边界。

**Q6：上下文超长怎么办？**
> 字符级预算 + 以"完整工具回合"为单位的压缩；固定保留系统消息与最新任务块，旧块整体丢弃，
> 避免把半个工具回合喂给模型。

**Q7：审计里会不会泄露敏感内容？**
> `AuditLogger.redact()` 递归隐藏 credential 类字段；自由文本只记字符数；SQLite 只存
> 结构化元数据与长度占位。API Key 全程只进内存。

**Q8：最大的坑 / 学到什么？**
> 可选回答方向：TOCTOU 竞态（审批到写入之间目标可能被替换，需要目录绑定 + identity 核验）、
> 同步 Agent 循环与异步 TUI 的线程桥接、Windows/POSIX 路径与句柄语义差异。

---

## 8. 面试讲解建议（STAR 式）

1. **先讲威胁模型**（30 秒）：LLM 自主写文件的最大风险是"越界 + 不可控"，所以设计目标是
   "边界 + 审批 + 可审计"。
2. **再讲架构**（1 分钟）：一条数据流图走完"任务 → Agent 循环 → Provider/工具 → 审批 → 审计"。
3. **深入一个亮点**（1-2 分钟）：首选**目录绑定防 TOCTOU**（最能体现工程深度），
   其次**协议策略模式**或**审计脱敏**。
4. **主动讲缺陷**（30 秒）：挑 2-3 个（如无沙箱、内存账本、检索性能），并给出方案，
   展示"知道边界 + 有演进路线"。
5. **备好数字**：源码 ~6.4k 行、测试 417 项、10 个工具（9 动作 + finish）、3 家 Provider、依赖仅 2 个、
   默认 30 轮、80k 字符预算。

---

## 9. 文档维护约定

- 改动 `src/tricoder/` 结构或新增模块时，同步更新第 2.2 节模块表。
- 新增/删除工具时，更新第 1 节工具清单与第 2.3 节扩展点。
- 安全机制变更时，重点检查第 4 节（这是面试核心，必须保持准确）。
- 测试数量变化时更新第 1/8 节的数字。
- 引用一律使用类名/函数名，不使用具体行号。

---

*相关文档：`README.md`（使用）、`docs/framework/tui-framework.md`（TUI 决策）、
`docs/open-source-assessment.md`（开源对比）、`project.md`（进度与验证）、
`docs/superpowers/`（设计与计划历史）。*
