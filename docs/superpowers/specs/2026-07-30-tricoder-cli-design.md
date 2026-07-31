# TriCoder CLI MVP 设计

## 目标

实现一个适合写入简历、容易讲清楚内部原理的命令行 Coding Agent。它能在指定工作区内分析代码、提出修改、经用户审批后编辑文件和执行低风险检查命令，并统一接入 OpenAI、DeepSeek、GLM 三类 API。

## 架构

系统采用线性 Agent 循环：

1. CLI 读取任务、Provider 与工作区参数。
2. Agent 将工具协议和用户任务发送给模型。
3. 模型返回单个 JSON 动作。
4. 工具层校验路径和权限；读取操作自动执行，写入和命令操作请求审批。
5. 工具结果回填消息历史，直到模型调用 `finish` 或达到最大轮数。

主要边界：

- `config`：解析命令行、环境变量、项目配置和安全默认值。
- `providers`：封装三家 OpenAI-compatible Chat Completions HTTP 调用。
- `policy`：路径隔离、敏感文件拒绝和命令风险判断。
- `tools`：文件查看、文本搜索、精确编辑、命令执行和任务结束。
- `agent`：维护线性消息历史并调度工具。
- `audit`：保存脱敏后的 JSONL 操作轨迹。
- `cli`：提供 `run` 和 `doctor` 命令。

## API 适配

三家 Provider 使用同一个 `ModelProvider.complete(messages)` 接口，通过 API Key、Base URL 和模型名配置差异：

| Provider | 环境变量 | 默认 Base URL | 默认模型 |
|---|---|---|---|
| OpenAI | `OPENAI_API_KEY` | `https://api.openai.com/v1` | `gpt-5` |
| DeepSeek | `DEEPSEEK_API_KEY` | `https://api.deepseek.com` | `deepseek-v4-flash` |
| GLM | `ZAI_API_KEY` | `https://open.bigmodel.cn/api/paas/v4` | `glm-5.2` |

GLM Coding Plan 可通过 `--base-url https://open.bigmodel.cn/api/coding/paas/v4` 覆盖。API Key 从进程环境变量、显式 `--env-file` 或工作区根目录下被 Git 忽略的 `.env.local` 读取，不写入 `.tricoder.toml`、可提交模板或日志；进程环境变量优先。显式文件可位于目标工作区之外，以便多个测试沙盒复用且不复制密钥。

## 工具与权限

- `list_files`、`read_file`、`search_text`：工作区内自动执行。
- `edit_file`：采用旧文本精确匹配替换；展示 diff 并经确认后原子写入。
- `run_command`：仅允许测试、静态检查以及只读 Git 命令；展示命令并经确认后执行。
- `finish`：返回任务摘要。
- `--read-only`：禁用编辑和命令执行。

所有路径在使用前解析为规范绝对路径，必须位于工作区内。拒绝访问 `.git`、`.env`、密钥文件和常见凭据目录。MVP 直接拒绝删除、依赖安装、Git 写操作、Shell 管道与重定向，不提供审批绕过。

## 错误与审计

- API 超时、限流和服务端错误最多指数退避重试三次。
- 模型返回非法 JSON 时反馈格式错误并允许修正；连续失败后终止。
- 精确编辑发生冲突时不写文件，要求模型重新读取。
- 命令执行设置超时，输出长度有上限。
- 最大 Agent 轮数可配置。
- JSONL 轨迹记录工具名、脱敏参数、结果摘要、状态和耗时，不记录隐藏推理或 API Key。

## 测试与验收

全部测试使用标准库 `unittest` 和 Fake Provider，无需真实 API Key：

- 配置优先级与 Provider 请求契约。
- 路径逃逸、敏感文件和符号链接防护。
- 读取、搜索、审批、精确替换、原子写入和命令限制。
- 完整的“读取—编辑—检查—完成”Agent 循环。
- `doctor`、`run`、只读模式和错误退出码。

验收场景：用户选择任一 Provider，Agent 检查本地项目，提出并经批准执行修改，经批准运行测试，最终输出变更、验证结果和脱敏轨迹位置。
