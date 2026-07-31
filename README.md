# TriCoder CLI

TriCoder CLI 是一个需要人工审批的本地 Coding Agent。它支持 OpenAI-compatible Chat Completions API，并提供一次性 `run` 与持续交互式会话两种入口。

## 安装与帮助

需要 Python 3.11+。在开发环境安装项目后：

```powershell
python -m pip install -e .
$env:PYTHONPATH = "src"
python -B -m tricoder --help
python -B -m tricoder doctor --help
python -B -m tricoder run --help
python -B -m tricoder chat --help
```

已安装命令入口时，也可以直接运行：

```powershell
tricoder --help
```

`--help` 只显示帮助，不会进入交互模式。

## 配置与密钥安全

将 `.env.example` 复制为仅供本机使用的 `.env.local`，再在自己的编辑器中填写所选服务商的 API Key：

```powershell
Copy-Item .env.example .env.local
python -B -m tricoder doctor --provider deepseek --workspace .
```

不要将真实 Key 写入 `.tricoder.toml`、`.env.example`、日志、SQLite 数据库或 Git。也不要把 `.env.local` 提交到版本库；该文件仅应保留在本机。可以用 `--env-file` 显式指定密钥文件；进程环境变量的优先级更高。

配置优先级为：命令行参数、进程环境变量、本地密钥文件、项目 `.tricoder.toml`、内置默认值。Provider 的默认 Key 变量和模型如下：

| Provider | API Key 环境变量 | 默认模型 |
| --- | --- | --- |
| OpenAI | `OPENAI_API_KEY` | `gpt-5` |
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek-v4-flash` |
| GLM | `ZAI_API_KEY` | `glm-5.2` |

三家 Provider 都支持原生 structured tool calling。TriCoder 默认使用 `native`：向
Chat Completions 请求发送工具定义，由 Provider 适配器把厂商响应归一化为
`ProviderResponse`，Agent 每轮只接受一个结构化工具调用，并用对应的
`tool_call_id` 回填工具结果。

| Provider | 原生工具调用 | 当前适配说明 |
| --- | --- | --- |
| OpenAI | 支持 | 发送工具定义和自动工具选择；客户端关闭并行调用 |
| DeepSeek | 支持 | 发送工具定义和自动工具选择 |
| GLM | 支持 | 发送工具定义和自动工具选择 |

`TRICODER_TOOL_PROTOCOL` 只接受 `native` 或 `legacy_json`，默认是 `native`。
进程环境变量和 `.env.local` 都优先于项目 `.tricoder.toml`，其中进程环境变量
优先级最高。`.env.example` 中的协议行默认已注释，因此复制模板后，下面的
TOML 回滚可以直接生效。也可以先用临时进程环境变量显式回滚：

```powershell
$env:TRICODER_TOOL_PROTOCOL = "legacy_json"
python -B -m tricoder doctor --provider openai --workspace . --no-color
```

也可以在项目配置中持久回滚：

```toml
[agent]
tool_protocol = "legacy_json"
```

`doctor` 会显示当前工具协议，但只显示 Key 的变量名和掩码，不显示 Key
内容。排查问题时不要把 Key 粘贴到命令参数、日志、Issue 或聊天记录中。
确认 Provider 的原生协议兼容后，把配置改回 `native`。如果曾主动设置进程
覆盖，可运行 `Remove-Item Env:TRICODER_TOOL_PROTOCOL`；如果在 `.env.local`
取消注释并设置了该变量，则还必须删除该行、重新注释或改值，否则它仍会覆盖
项目 TOML。

可选的项目配置示例：

```toml
[agent]
model = "glm-5.2"
max_rounds = 10
max_context_chars = 80000
timeout = 30
tool_protocol = "native"

[providers.glm]
base_url = "https://open.bigmodel.cn/api/coding/paas/v4"
```

## 使用方式

先用不发送 API 请求的 `doctor` 检查本地配置：

```powershell
$env:PYTHONPATH = "src"
python -B -m tricoder doctor --provider deepseek --workspace . --no-color
```

一次性运行任务：

```powershell
python -B -m tricoder run "修复重复提交问题并运行测试" `
  --provider deepseek `
  --workspace D:\path\to\project `
  --max-context-chars 60000
```

只读分析不会编辑文件或执行命令：

```powershell
python -B -m tricoder run "分析项目结构和潜在风险" `
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

## 交互命令与 Session

斜杠命令始终只在本地处理，不会发送给 Provider：

| 命令 | 行为 |
| --- | --- |
| `/help` | 显示命令、参数和示例。 |
| `/status` | 显示当前 Session、工作区、Provider、模型、只读、验证及上下文状态。 |
| `/model` | 显示 OpenAI、DeepSeek、GLM 的模型并按序号切换。 |
| `/clear` | 仅在输入 `y` 或 `yes` 后清除当前 Session 的运行时上下文和持久化摘要。 |
| `/session` | 列出全部 Session，并按序号选择。 |
| `/session new <名称>` | 用当前工作区、Provider 和模型创建并切换到新 Session。 |
| `/session current` | 显示当前 Session 的详细信息。 |
| `/session rename <名称>` | 重命名当前 Session。 |
| `/exit` | 保存安全记忆并退出。 |

`/session` 切换到其他工作区时会显示目标绝对路径，必须明确输入 `y` 或 `yes` 才会继续。切换会先构建并验证目标配置、策略、工具和 Agent；任何一步失败都会保留原 Session 和原工作区。

`/clear` 不删除 Session，不会改动目标工作区中的文件，也保留 Provider、模型、工作区、修改文件元数据和审计记录；它只清除当前会话的消息历史和可持久化摘要。

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

当前版本不支持 Session 删除、命令插件、跨设备/云同步、向量检索、自动补全，也不支持多个进程同时编辑同一个 Session。

## 审计、上下文与退出码

每次 `run` 会写入 JSONL 审计文件。默认目录：Windows 为 `%LOCALAPPDATA%\TriCoder\runs`，其他系统为 `$XDG_STATE_HOME/tricoder/runs`；可用 `--audit-dir` 覆盖。只读模式下，审计目录不能位于目标工作区中。

`--max-context-chars` 限制每次发送给模型的上下文大小。固定 system/user 消息会保留，历史按完整交互轮次截断，避免保留半个工具回合。

`run` 的退出码：`0` 表示任务满足完成条件，`1` 表示任务未完成或最后验证失败，`2` 表示配置或运行前审计准备失败。

## 新增 Provider 适配器

新增 Provider 时保持边界最小：

1. 在 `src/tricoder/config.py` 注册默认 Key 环境变量、HTTPS Base URL、模型和
   允许的官方 Base URL。
2. 在 `src/tricoder/providers.py` 声明 `ProviderCapabilities` 并注册工厂。若不是
   OpenAI-compatible 协议，实现 `ModelProvider.complete(messages, tools)`。
3. `complete()` 返回归一化的 `ProviderResponse`（其中包含 `ToolCall`）；厂商
   响应无法解析或不满足协议时抛出 `ProviderProtocolError`。不要把厂商原始
   响应或认证头传给 Agent、日志或终端。
4. 在 `src/tricoder/cli.py` 的 `--provider` choices，以及
   `src/tricoder/ui.py` 的 Provider label、帮助和选择列表等公开注册点加入名称。
5. 为配置、请求序列化、响应解析、协议错误、UI/CLI 脱敏输出与交互选择补测试。

只有在适配器确实验证了原生工具调用时才声明
`native_tool_calling=True`。`legacy_json` 是显式兼容回滚路径，不应成为新
Provider 绕过结构化响应适配的默认实现。

## 测试

测试不需要网络或真实 API Key：

```powershell
$env:PYTHONPATH = "src"
python -B -m unittest discover -s tests -v
python -B -m compileall -q src tests
```

## 开源参考

项目仅参考 mini-swe-agent、Aider、OpenCode 和 OpenHands 的公开高层设计思想，未复制或集成其代码。比较与复用边界见 [docs/open-source-assessment.md](docs/open-source-assessment.md)。
