# TriCoder 交互式斜杠命令与独立会话设计

## 目标

为 TriCoder 增加接近 Codex CLI 的持续交互入口。用户可以直接运行 `tricoder` 或 `tricoder chat`，通过普通文本向 Agent 提交任务，通过 `/命令` 在本地控制模型、会话和运行状态。

首版重点是可靠的会话控制，不实现命令插件系统、向量记忆或复杂终端 UI。

## 启动方式与兼容性

以下两个入口进入同一个交互 Shell：

```powershell
tricoder
tricoder chat
```

现有入口保持兼容：

```powershell
tricoder doctor ...
tricoder run "任务" ...
```

`tricoder --help` 仍显示帮助，不进入交互模式。只有完全不带子命令和帮助参数时，裸 `tricoder` 才进入交互模式。

`chat` 接受 `run` 的工作区、Provider、模型、密钥文件、审计目录、上下文预算、轮数、超时、只读与颜色选项，但不接受位置参数 `task`。

进入交互模式时以当前目录或显式 `--workspace` 为起点：

- 优先恢复该工作区最近更新的 Session。
- 该工作区没有 Session 时创建名为 `default` 的 Session。
- 新 Session 使用显式 Provider；未显式指定时使用 `openai`。
- 显式 `--provider` 或 `--model` 在配置验证成功后覆盖恢复 Session 的选择并持久化。
- 其他工作区的 Session 只能通过 `/session` 列表和跨工作区确认切换，不会在启动时自动跳转。

## 架构

输入处理链路：

```text
用户输入
  ├─ 以 / 开头 → CommandRouter → 本地命令处理
  └─ 普通文本  → 当前 Session → CodingAgent
```

新增模块：

- `shell.py`：维护交互循环、当前 Session、运行时上下文与安全退出。
- `commands.py`：只负责解析和分发斜杠命令，不直接访问 SQLite 或调用模型。
- `sessions.py`：提供 SQLite 持久化、Session 列表、创建、重命名、切换和混合记忆。

`cli.py` 负责解析入口参数、建立初始配置并装配 Shell。Agent 层新增：

- `SessionContext`：不可变的会话消息快照和持久化摘要。
- `SessionTurnResult`：一次运行的 `RunResult` 与更新后的 `SessionContext`。
- `CodingAgent.run_with_context(task, context)`：供交互 Shell 使用。

现有 `CodingAgent.run(task)` 使用空上下文调用同一内部循环并继续只返回 `RunResult`，因此现有单次 `run` 入口保持兼容。

斜杠命令只在本地执行，永远不发送给模型。未知斜杠命令返回本地错误并提示 `/help`。

## 本地命令

首版支持：

| 命令 | 行为 |
| --- | --- |
| `/help` | 显示全部命令、参数和示例 |
| `/status` | 显示 Session、工作区、Provider、模型、只读状态、验证状态和上下文占用 |
| `/model` | 列出 OpenAI、DeepSeek、GLM 及配置模型，通过序号切换 |
| `/clear` | 经确认后清除当前 Session 的运行时上下文与持久化摘要 |
| `/session` | 列出全部 Session，通过序号选择 |
| `/session new <名称>` | 基于当前工作区、Provider 和模型创建并切换到新 Session |
| `/session current` | 显示当前 Session 详情 |
| `/session rename <名称>` | 重命名当前 Session |
| `/exit` | 保存安全记忆并退出 |

不支持的子命令、额外参数或缺失参数返回稳定的命令错误，不调用 Provider。

### `/model`

`/model` 显示三个 Provider、各自解析后的模型名和当前选择。用户输入序号后，Shell 使用当前 Session 的工作区和启动选项重新调用配置加载逻辑：

- 对应 API Key 或配置缺失时保持原选择并显示错误。
- 切换成功后更新当前 Session 的 Provider 与模型。
- 新 Provider 使用其已解析的官方或显式配置地址。
- 切换不清除当前 Session 记忆；后续普通消息使用新 Provider。

### `/clear`

`/clear` 必须明确输入 `y` 或 `yes`：

- 清除当前进程中的完整消息上下文。
- 清除 SQLite 中该 Session 的摘要和最近任务记忆。
- 保留 Session 记录、工作区、Provider、模型、修改文件路径、审计日志和其他 Session。
- 不删除或修改目标工作区中的文件。

## Session 模型

每个 Session 使用 UUID 作为稳定标识。名称允许重复，因为列表同时显示工作区和更新时间。

名称规则：

- 去除首尾空白后长度为 1–50 个 Unicode 可显示字符。
- 拒绝换行、制表符和其他控制字符。

Session 持久化字段：

- UUID、名称。
- 工作区绝对路径。
- Provider、模型名。
- 创建与更新时间，使用 UTC ISO 8601。
- 最近任务摘要。
- 压缩后的会话记忆。
- 用户长期要求的安全摘要。
- 修改文件相对路径列表。
- 最近验证状态。

不得持久化：

- API Key 或 Authorization Header。
- `read_file`、`search_text` 和命令的完整输出。
- 完整源码正文。
- 完整命令文本。
- Provider 原始响应或模型动作 JSON。

## 混合记忆

当前进程内，每个已打开 Session 拥有独立的完整消息历史。切换 Session 后，Shell 保存当前运行时上下文并恢复目标 Session 的上下文；两个 Session 的消息、Provider、模型和工作区不得共享可变对象。

关闭 CLI 后只持久化安全摘要与结构化元数据：

- 使用每次 `RunResult.summary` 形成有长度上限的会话摘要。
- 从用户任务中只提炼并保存有长度上限的长期要求摘要，不保存用户粘贴的源码正文。
- 保存修改文件路径、验证状态和最近任务结果。
- 摘要经过现有审计级自由文本脱敏，并限制最大字符数。
- 不持久化工具结果或完整消息数组。

从 SQLite 恢复 Session 时，Agent 收到：

- system prompt。
- 一条标记为“持久化会话摘要”的上下文消息。
- 新的用户任务。

Agent 需要源码时必须重新调用读取工具。恢复后的摘要进入现有上下文预算计算。

## SQLite SessionStore

数据库默认位置：

- Windows：`%LOCALAPPDATA%\TriCoder\sessions.db`；缺失 `LOCALAPPDATA` 时使用 `%USERPROFILE%\AppData\Local\TriCoder\sessions.db`。
- 非 Windows：`$XDG_STATE_HOME/tricoder/sessions.db`；缺失时使用 `~/.local/state/tricoder/sessions.db`。

数据库位于系统状态目录，不写入目标工作区。API Key 不进入数据库。

首版使用两个表：

```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    workspace TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE session_memory (
    session_id TEXT PRIMARY KEY,
    summary TEXT NOT NULL DEFAULT '',
    requirements_summary TEXT NOT NULL DEFAULT '',
    last_task_summary TEXT NOT NULL DEFAULT '',
    modified_files_json TEXT NOT NULL DEFAULT '[]',
    verification TEXT NOT NULL DEFAULT '未运行',
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
```

初始化使用幂等 `CREATE TABLE IF NOT EXISTS`，开启外键约束。写操作使用显式事务。首版不支持多进程同时修改同一 Session，也不实现删除，因此不依赖级联删除行为。

数据库无法初始化时，交互入口返回配置错误。运行中持久化失败时：

- 当前内存 Session 保持可用。
- 明确显示“本次记忆未持久化”。
- 不把失败状态显示为已保存。
- `/exit` 再尝试一次保存，失败时以非零退出码结束。

## `/session` 选择流程

无参数 `/session` 按更新时间倒序列出全部 Session：

```text
  #  会话              工作区                 模型          更新时间
  1  auth-fix          D:\projects\shop       deepseek      10:42
  2  cli-refactor      D:\projects\tricoder   glm           昨天
```

用户输入序号选择，空输入取消。

同工作区切换立即执行。跨工作区切换必须显示目标绝对路径，并明确输入 `y` 或 `yes`。确认后：

1. 保存当前 Session 的安全摘要和结构化状态。
2. 为目标 Session 重新加载配置。
3. 重新建立 `WorkspacePolicy`、`CommandPolicy`、工具注册表和 Agent。
4. 仅在全部步骤成功后替换当前 Session。

任一步失败时保持原 Session 和原工作区，不出现半切换状态。

## Shell 行为

- 空输入忽略。
- 普通文本交给当前 Session 的 Agent，并显示现有审批、工具过程和完成摘要。
- `Ctrl+C` 在输入阶段清空当前输入并继续；Agent 执行阶段安全停止当前任务，保留 Session。
- EOF，包括 Windows `Ctrl+Z`，等同于 `/exit`。
- `/exit` 保存当前 Session 后退出。
- 未知命令或命令参数错误不调用 API。

首版交互输入使用现有 `input_fn` 注入边界，保持可测试性，不引入 prompt-toolkit 等新依赖。

## 错误处理

- Session 名称无效：本地报错，不写数据库。
- 序号不存在：本地报错，保持当前 Session。
- Provider 配置失败：保持原模型和 Provider。
- 跨工作区配置失败：保持原 Session、工具和策略。
- SQLite 读取损坏：交互入口以配置错误退出，不静默覆盖数据库。
- SQLite 运行时写入失败：保留内存状态并显示未持久化警告。
- Agent、审计和工具错误沿用现有安全停止语义。

## 测试

### 命令解析

- 支持全部已声明命令和参数。
- 未知命令、缺失参数、额外参数返回错误。
- 斜杠命令不调用 Provider。

### SessionStore

- SQLite 初始化幂等。
- 创建、列表、重命名和按 ID 读取。
- 两个 Session 的记忆互不影响。
- 名称校验和事务回滚。
- 数据库中不出现测试 API Key、完整源码、完整命令或模型原始动作。

### InteractiveShell

- 裸 `tricoder` 与 `tricoder chat` 进入相同 Shell。
- `--help` 不进入 Shell。
- `/session` 编号选择与取消。
- 跨工作区切换确认、拒绝和失败回滚。
- `/model` 成功与配置失败回滚。
- `/clear` 确认、拒绝和其他 Session 不受影响。
- EOF、输入阶段 `Ctrl+C` 与 `/exit`。

### 集成

- 两个 Session 分别执行不同任务，消息、模型、工作区和摘要不串联。
- 退出后重新启动并恢复安全摘要。
- 数据库始终位于系统状态目录，目标工作区零 Session 写入。

## 非目标

- `/session delete`。
- 命令插件发现与第三方命令。
- 多进程并发编辑同一 Session。
- 云同步、账户系统或远程 Session。
- 向量数据库、Embedding 或语义检索。
- 自动生成 Session 名称。
- 命令自动补全、方向键选择或全屏终端 UI。
