# TriCoder CLI

[![CI](https://github.com/PooooLish/Tricoder-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/PooooLish/Tricoder-Agent/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](https://www.python.org/)

TriCoder CLI 是一个强调可控执行、会话记忆和多模型适配的本地 Coding Agent MVP。它统一接入 OpenAI、DeepSeek 与 GLM 的原生 structured tool calling，并在文件写入和命令执行前要求人工审批。

## 核心亮点

- **统一 Provider 边界**：OpenAI、DeepSeek、GLM 响应统一归一化为内部 `ProviderResponse` 与 `ToolCall`。
- **原生工具调用**：默认使用厂商 structured tool calling，并保留显式 `legacy_json` 回滚协议。
- **可控本地执行**：读取、检索（`search_text` 支持正则、基础 `.gitignore` 常用语义（尾随 `/` 目录规则按任意层级匹配）与二进制/超大文件跳过，正则长度与单行长度受限以防灾难性回溯；`glob_files` 按相对模式定位文件，pattern 长度、`**` 数量与扫描结果规模均受限）、编辑、创建文件和运行受限命令；`git_diff` 只读展示工作区未提交变更统计；Provider 原生 `apply_patch` 可在一次审批中应用受限的多文件 unified diff，只允许修改或创建文件，不支持删除或重命名；`--read-only` 禁止 `edit_file`、`create_file`、`apply_patch` 等写入；写操作与命令执行需要人工审批。
- **独立 Session 记忆**：每个 Session 保存独立工作区、Provider、模型、安全摘要和结构化状态。
- **本地斜杠命令**：`/session`、`/model`、`/status`、`/clear` 等命令不会发送给 Provider。
- **可审计与可验证**：运行过程写入 JSONL 审计记录，并由跨平台自动化测试覆盖核心边界。

## Provider 用量与 KV Cache 指标

每次 Provider 响应完成后，只有当服务商 `usage` 至少包含一个有效用量字段时，CLI 才显示该轮的输入、缓存和输出 token 用量行；同一任务的累计用量由各轮已返回的指标相加得到。TriCoder 只归一化并展示/审计这些服务商返回的用量数据，不会在本地保存或管理 KV Cache。

缓存相关字段是观测值，不是本地缓存状态：单个用量字段缺失或无效时，界面会显示 `-`，不会将未知值当作 `0`；整个 `usage` 缺失、无效或所有字段均无效时，本轮不显示用量行。缓存命中率仅在服务商同时返回可计算的输入和缓存 token 时显示。

本版本没有启用显式 cache key，也没有实现延长缓存保留期等缓存策略；实际缓存行为、命中与保留规则均由所选 Provider 决定。

## 5 分钟快速体验

需要 Python 3.11+。以下为 Windows PowerShell 主路径：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env.local
python -m tricoder doctor --provider deepseek --workspace . --no-color
python -m tricoder chat --provider deepseek --workspace .
```

`.env.local` 只在本机填写；Bash 使用 `source .venv/bin/activate` 和 `cp .env.example .env.local`。`doctor` 不发送模型请求；也可直接运行 `tricoder` 进入默认交互模式。

## 架构与职责

```mermaid
flowchart LR
    U["用户输入"] --> CLI["CLI / Slash Commands"]
    CLI --> S["Session Runtime"]
    S --> DB[("SQLite 安全摘要")]
    CLI --> A["Agent Core"]
    A <--> P["Provider Adapter"]
    P <--> API["OpenAI / DeepSeek / GLM"]
    A <--> T["Tool Runtime"]
    T --> W["目标工作区"]
    T --> J["JSONL 审计"]
```

斜杠命令只在本地处理，不会发送给 Provider；普通任务才进入 Agent、Provider 与工具运行时组成的循环。

## 配置与密钥安全

将 `.env.example` 复制为仅供本机使用的 `.env.local`，再在自己的编辑器中填写所选服务商的 API Key。不要将真实 Key 写入 `.tricoder.toml`、`.env.example`、日志、SQLite 数据库或 Git；也不要把 `.env.local` 提交到版本库。可以用 `--env-file` 显式指定密钥文件；进程环境变量的优先级更高。

配置优先级为：命令行参数、进程环境变量、本地密钥文件、项目 `.tricoder.toml`、内置默认值。Provider 的默认 Key 变量和模型如下：

| Provider | API Key 环境变量 | 默认模型 |
| --- | --- | --- |
| OpenAI | `OPENAI_API_KEY` | `gpt-5` |
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek-v4-flash` |
| GLM | `ZAI_API_KEY` | `glm-5.2` |

## 原生工具协议

三家 Provider 都支持原生 structured tool calling。TriCoder 默认使用 `native`：向 Chat Completions 请求发送工具定义，由 Provider 适配器把厂商响应归一化为 `ProviderResponse`，Agent 每轮只接受一个结构化工具调用，并用对应的 `tool_call_id` 回填工具结果。

| Provider | 原生工具调用 | 当前适配说明 |
| --- | --- | --- |
| OpenAI | 支持 | 发送工具定义和自动工具选择；客户端关闭并行调用 |
| DeepSeek | 支持 | 发送工具定义和自动工具选择 |
| GLM | 支持 | 发送工具定义和自动工具选择 |

`TRICODER_TOOL_PROTOCOL` 只接受 `native` 或 `legacy_json`，默认是 `native`。环境变量和 `.env.local` 都优先于项目 `.tricoder.toml`，其中进程环境变量优先级最高。`.env.example` 中的协议行默认已注释，因此复制模板后，下面的 TOML 回滚可以直接生效：

```toml
[agent]
tool_protocol = "legacy_json"
```

也可以用临时进程环境变量显式回滚：

```powershell
$env:TRICODER_TOOL_PROTOCOL = "legacy_json"
python -m tricoder doctor --provider openai --workspace . --no-color
```

`doctor` 会显示当前工具协议，但只显示 Key 的变量名和掩码，不显示 Key 内容。排查问题时不要把 Key 粘贴到命令参数、日志、Issue 或聊天记录中。确认 Provider 的原生协议兼容后，把配置改回 `native`。如果曾主动设置进程覆盖，可运行 `Remove-Item Env:TRICODER_TOOL_PROTOCOL`；如果在 `.env.local` 取消注释并设置了该变量，则还必须删除该行、重新注释或改值，否则它仍会覆盖项目 TOML。

可选的项目配置示例：

```toml
[agent]
model = "glm-5.2"
max_rounds = 10
max_context_chars = 80000
timeout = 30
tool_protocol = "native"
plan = true

[providers.glm]
base_url = "https://open.bigmodel.cn/api/coding/paas/v4"
```

## 执行前规划（Planner-Executor）

每个任务在进入工具循环前默认有一次**规划阶段**（round 0，不带工具）：Agent 基于任务
与会话摘要先输出 3–8 步执行计划（JSON 或 Markdown 列表），计划作为消息注入后续执行，
让多步任务按计划推进。规划阶段只生成文本、无副作用，不写文件、不执行命令。

- 规划失败（Provider 错误 / 解析失败）时**降级为无计划执行**，不阻塞任务。
- 计划文本不写入 SQLite 或审计原文（审计只记录步骤数与字符数）。
- 关闭规划：`--no-plan` 命令行参数，或环境变量 `TRICODER_PLAN=0`，或项目配置
  `[agent] plan = false`（命令行 > 环境变量 > 项目配置 > 默认开启）。
- 规划增加一次模型调用与 token 成本，可用上述方式关闭。

## 使用方式

`--help` 只显示帮助，不会进入交互模式：

```powershell
python -m tricoder --help
python -m tricoder doctor --help
python -m tricoder run --help
python -m tricoder chat --help
```

一次性运行任务：

```powershell
python -m tricoder run "修复重复提交问题并运行测试" `
  --provider deepseek `
  --workspace D:\path\to\project `
  --max-context-chars 60000
```

只读分析不会编辑文件或执行命令：

```powershell
python -m tricoder run "分析项目结构和潜在风险" `
  --provider openai `
  --workspace D:\path\to\project `
  --read-only
```

进入持续交互会话有两个等价入口：

```powershell
tricoder
tricoder chat --provider deepseek --workspace D:\path\to\project
```

裸 `tricoder` 使用当前目录作为工作区；`tricoder chat` 支持 `run` 的工作区、Provider、模型、密钥文件、审计目录、上下文预算、轮数、超时、只读与颜色选项，但没有任务位置参数。

基于 Textual 的本地 TUI（组件化消息流、模态审批，安全边界与 `chat` 一致）：

```powershell
python -m tricoder tui --provider deepseek --workspace D:\path\to\project
```

`tui` 与 `chat` 接受相同选项；`Ctrl+Q` 保存记忆并退出，`Ctrl+C` 清空输入，写操作与命令执行在模态中明确确认。

## 交互命令与 Session

斜杠命令始终只在本地处理，不会发送给 Provider：

| 命令 | 行为 |
| --- | --- |
| `/help` | 显示命令、参数和示例。 |
| `/status` | 显示当前 Session、工作区、Provider、模型、只读、验证及上下文状态。 |
| `/model` | 显示 OpenAI、DeepSeek、GLM 的模型并按序号切换。 |
| `/clear` | 仅在输入 `y` 或 `yes` 后清除当前 Session 的运行时上下文和持久化摘要。 |
| `/diff` | 本地展示当前 Session 最近一次非空任务的正向 unified diff，不发送给 Provider。 |
| `/undo` | 先本地展示当前 Session 最近一次非空任务的完整反向 unified diff；仅在输入 `y` 或 `yes` 后尝试撤销整组变更。任何外部内容、权限模式或文件身份冲突都会拒绝全部写入；`--read-only` 会在预览或确认前拒绝撤销。 |
| `/session` | 列出全部 Session，并按序号选择。 |
| `/session new <名称>` | 用当前工作区、Provider 和模型创建并切换到新 Session。 |
| `/session current` | 显示当前 Session 的详细信息。 |
| `/session rename <名称>` | 重命名当前 Session。 |
| `/exit` | 保存安全记忆并退出。 |

`/session` 切换到其他工作区时会显示目标绝对路径，必须明确输入 `y` 或 `yes` 才会继续。切换会先构建并验证目标配置、策略、工具和 Agent；任何一步失败都会保留原 Session 和原工作区。

`/clear` 不删除 Session，不会改动目标工作区中的文件，也保留 Provider、模型、工作区、修改文件元数据和审计记录；它只清除当前会话的消息历史和可持久化摘要。

## 任务级变更预览与撤销

每次非空 Agent 任务的文件净变更只保存在当前进程中对应 Session 的内存账本里。`/diff` 正向预览最近一条非空任务，在 `--read-only` 下仍可使用；`/undo` 先完整预览反向 diff，再以 `y` 或 `yes` 明确确认。`--read-only` 会在预览或确认前稳定拒绝 `/undo`。撤销会重新核验每个目标的内容、权限模式与文件身份；只要发现任一外部冲突，就拒绝全部写入。经确认后，撤销也可能删除由该任务创建的文件。

源码快照以及本地生成的正向/反向 diff 不会写入 SQLite、JSONL 审计记录或发送给 Provider。`apply_patch` 的补丁文本由 Provider 生成并作为原生工具调用参数进入当前进程的消息上下文；同一任务继续推理时，它可能随后续轮次的消息历史再次发送给 Provider，但不会写入 SQLite 或 JSONL 审计记录。内存账本总预算为 2,000,000 个字符，进程重启后历史即消失。MVP 不提供 `/redo`、多级撤销、按历史记录选择撤销、持久化撤销历史，也不依赖 Git；`apply_patch` 同样不支持文件删除或重命名补丁。

## Session 数据与恢复

SQLite 数据库位于系统状态目录，不会写入目标工作区：

- Windows：`%LOCALAPPDATA%\TriCoder\sessions.db`；若未设置 `LOCALAPPDATA`，使用 `%USERPROFILE%\AppData\Local\TriCoder\sessions.db`。
- 其他系统：`$XDG_STATE_HOME/tricoder/sessions.db`；若未设置 `XDG_STATE_HOME`，使用 `~/.local/state/tricoder/sessions.db`。

每个 Session 独立保存名称、工作区绝对路径、Provider、模型、时间戳、修改文件路径、验证状态和受限长度的安全摘要。进程运行期间，每个已打开 Session 有独立的完整消息上下文；重启后只恢复安全摘要和结构化元数据。Agent 如需源码，必须重新调用读取工具。

会话持久化只保存受控结构化元数据，不保存任何用户任务或模型 `RunResult.summary` 的自由文本原文。无论内容是空白、中英文自然语言、源码、命令、工具输出、Provider 原始响应、动作 JSON、认证信息还是完整消息历史，SQLite 中的 `requirements_summary` 都只保存长度占位；运行结果只保存固定格式的成功/失败、修改文件数量和规范化验证状态。成功编辑或创建的文件路径由工作区策略解析后以规范相对路径保存，不会保存原始绝对路径或 `..` 形式。这个策略不依赖“看起来像代码或命令”的启发式判断。

完整消息上下文仅保留在当前进程的 Session 中，CLI 仍会在当前轮显示 `RunResult.summary`；重启后只能恢复上述结构化元数据和长度占位。该边界仍需配合工作区权限和本地存储权限管理。

如果数据库无法安全初始化，交互入口会以配置错误退出（退出码 `2`），不会改用其他工作区。运行中持久化失败时，当前内存会话可继续使用，但界面会提示“本次记忆未持久化”；`/exit` 会再尝试保存，仍失败时以非零退出码结束。

## 运行边界

- 文件写入和命令执行都需要在终端明确输入 `y` 或 `yes` 审批；`--read-only` 会禁止这两类操作。
- Agent 只能访问指定工作区内的非敏感文件，越界或敏感路径会被拒绝。
- 允许的命令限于测试、静态检查和只读 Git 查询；审批不是操作系统或容器沙箱的替代品。
- 发送任务会将相关代码片段交给所选 Provider；只应在获准发送的项目中使用。

## 审计、上下文与退出码

每次 `run` 会写入 JSONL 审计文件。默认目录：Windows 为 `%LOCALAPPDATA%\TriCoder\runs`，其他系统为 `$XDG_STATE_HOME/tricoder/runs`；可用 `--audit-dir` 覆盖。只读模式下，审计目录不能位于目标工作区中。

`--max-context-chars` 限制每次发送给模型的上下文大小。固定 system/user 消息会保留，历史按完整交互轮次截断，避免保留半个工具回合。

`run` 的退出码：`0` 表示任务满足完成条件，`1` 表示任务未完成或最后验证失败，`2` 表示配置或运行前审计准备失败。

## 新增 Provider 适配器

新增 Provider 时保持边界最小：

1. 在 `src/tricoder/config.py` 注册默认 Key 环境变量、HTTPS Base URL、模型和允许的官方 Base URL。
2. 在 `src/tricoder/providers.py` 声明 `ProviderCapabilities` 并注册工厂。若不是 OpenAI-compatible 协议，实现 `ModelProvider.complete(messages, tools)`。
3. `complete()` 返回归一化的 `ProviderResponse`（其中包含 `ToolCall`）；厂商响应无法解析或不满足协议时抛出 `ProviderProtocolError`。不要把厂商原始响应或认证头传给 Agent、日志或终端。
4. 在 `src/tricoder/cli.py` 的 `--provider` choices，以及 `src/tricoder/ui.py` 的 Provider label、帮助和选择列表等公开注册点加入名称。
5. 为配置、请求序列化、响应解析、协议错误、UI/CLI 脱敏输出与交互选择补测试。

只有在适配器确实验证了原生工具调用时才声明 `native_tool_calling=True`。`legacy_json` 是显式兼容回滚路径，不应成为新 Provider 绕过结构化响应适配的默认实现。

## 测试

### 无密钥自动化测试

以下检查不联网，也不需要真实 API Key；GitHub Actions 会在 Windows/Linux 和 Python 3.11/3.12 上执行同样的验证：

```powershell
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

### 本地真实 API 冒烟测试

真实 API 测试只在开发者明确配置 `.env.local` 后本地执行，可能产生费用，也可能受 Provider 网络状态影响，因此不纳入 CI。读取、修改和命令执行练习应在 [`test/`](test/README.md) 沙盒中进行，不要放入真实密钥、私人数据或重要文件。

```powershell
python -m tricoder run "只读检查 smoke_demo.py，并说明 add 函数的行为" --provider openai --workspace test --read-only
```

将 `--provider` 分别替换为 `deepseek` 和 `glm` 即可验证三家 Provider。涉及创建文件或运行命令的测试会进入人工审批流程，测试目标必须留在 `test/`。

## 路线

以下方向尚未实现：Session 删除与导出、命令插件/自动补全、可选检索记忆、演示 GIF。

## 开源参考

项目仅参考 mini-swe-agent、Aider、OpenCode 和 OpenHands 的公开高层设计思想，未复制或集成其代码。比较与复用边界见 [docs/open-source-assessment.md](docs/open-source-assessment.md)。
