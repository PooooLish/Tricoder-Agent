# TriCoder CLI MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建支持 OpenAI、DeepSeek、GLM 的安全审批式命令行 Coding Agent MVP。

**Architecture:** 使用线性 Agent 消息循环和结构化 JSON 工具动作。Provider、策略、工具、审计与 CLI 保持独立，通过小型数据类和 Protocol 连接。

**Tech Stack:** Python 3.11+ 标准库、`argparse`、`urllib.request`、`tomllib`、`unittest`

**Implementation Status:** Tasks 1–5 completed locally on 2026-07-30. No dependency installation, Git initialization, commit, or publish action was performed.

## Global Constraints

- 项目目录固定为 `projects/tricoder-cli/`，不得访问外部旧工作区。
- 不安装依赖、不初始化 Git、不保存真实 API Key。
- 源码公共接口使用类型注解，关键安全逻辑使用规范且易懂的中文注释。
- 读取操作自动执行；编辑和低风险命令必须审批；危险操作直接拒绝。
- 所有自动化测试不得依赖网络或真实 API。

## 文件结构

- `pyproject.toml`：包元数据和 `tricoder` 命令入口。
- `src/tricoder/models.py`：消息、动作、工具结果和配置数据类。
- `src/tricoder/config.py`：Provider 默认值、环境变量和 TOML 配置合并。
- `src/tricoder/providers.py`：HTTP 传输抽象和三家 Provider 适配。
- `src/tricoder/policy.py`：工作区路径与命令安全策略。
- `src/tricoder/tools.py`：六个工具及执行上下文。
- `src/tricoder/audit.py`：轨迹脱敏与 JSONL 写入。
- `src/tricoder/agent.py`：动作解析、Agent 循环和工具调度。
- `src/tricoder/cli.py`、`src/tricoder/__main__.py`：CLI 命令。
- `tests/`：与上述模块一一对应的单元和集成测试。

---

### Task 1: 配置与 Provider 契约

**Files:**
- Create: `pyproject.toml`
- Create: `src/tricoder/__init__.py`
- Create: `src/tricoder/models.py`
- Create: `src/tricoder/config.py`
- Create: `src/tricoder/providers.py`
- Test: `tests/test_config.py`
- Test: `tests/test_providers.py`

**Interfaces:**
- Produces: `AppConfig`、`ProviderConfig`、`Message`、`ModelProvider.complete(messages)`、`load_config(...)`

- [ ] **Step 1: 编写配置优先级和三家请求契约的失败测试**

测试显式断言命令行覆盖 TOML、TOML 覆盖默认值、API Key 只来自环境变量；Fake Transport 捕获 `POST <base_url>/chat/completions`、Bearer 鉴权和 `model/messages` 请求体。

- [ ] **Step 2: 运行 `python -m unittest tests.test_config tests.test_providers -v`，确认因模块不存在而失败**

- [ ] **Step 3: 实现最小数据模型、配置合并和 HTTP Provider**

`load_config(provider, workspace, model=None, base_url=None, ...) -> AppConfig` 必须验证 Provider 名称、工作区目录和对应环境变量。`OpenAICompatibleProvider.complete(messages: list[Message]) -> str` 负责 JSON 编解码、响应字段校验，以及 429/5xx/超时重试。

- [ ] **Step 4: 重跑 Task 1 测试并确认通过**

### Task 2: 路径与命令策略

**Files:**
- Create: `src/tricoder/policy.py`
- Test: `tests/test_policy.py`

**Interfaces:**
- Consumes: `AppConfig.workspace`
- Produces: `WorkspacePolicy.resolve_path(path, must_exist)`、`CommandPolicy.validate(command)`

- [ ] **Step 1: 编写路径逃逸、敏感文件、符号链接和危险命令失败测试**

覆盖 `../`、工作区外绝对路径、`.git/config`、`.env`、指向外部的符号链接、管道/重定向、删除、安装依赖、Git 写操作；同时验证 `python -m unittest`、`pytest`、`ruff check`、`git status`、`git diff` 可通过。

- [ ] **Step 2: 运行 `python -m unittest tests.test_policy -v`，确认失败**

- [ ] **Step 3: 实现策略**

路径使用 `Path.resolve()` 与 `Path.is_relative_to()`；逐段匹配敏感名称。命令用 `shlex.split()` 解析且 `shell=False` 执行，只接受测试、静态检查和只读 Git 子命令。

- [ ] **Step 4: 重跑 Task 2 测试并确认通过**

### Task 3: 工具与审计

**Files:**
- Create: `src/tricoder/tools.py`
- Create: `src/tricoder/audit.py`
- Test: `tests/test_tools.py`
- Test: `tests/test_audit.py`

**Interfaces:**
- Consumes: `WorkspacePolicy`、`CommandPolicy`
- Produces: `ToolContext`、`ToolRegistry.execute(name, arguments)`、`AuditLogger.log(event)`

- [ ] **Step 1: 编写读取、搜索、审批、冲突编辑、原子写入、超时和脱敏测试**

Fake Approver 记录审批内容；编辑仅允许旧文本恰好匹配一次；拒绝审批后文件保持不变；日志中的 `api_key/token/password/authorization` 值必须替换为 `***`。

- [ ] **Step 2: 运行 `python -m unittest tests.test_tools tests.test_audit -v`，确认失败**

- [ ] **Step 3: 实现六个工具和 JSONL 审计**

读取与搜索限制字符数和结果数。编辑先生成 unified diff，再请求审批，并通过同目录临时文件加 `os.replace()` 原子提交。命令经策略和审批后用参数数组执行，限制超时与输出长度。

- [ ] **Step 4: 重跑 Task 3 测试并确认通过**

### Task 4: Agent 循环

**Files:**
- Create: `src/tricoder/agent.py`
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `ModelProvider.complete`、`ToolRegistry.execute`、`AuditLogger.log`
- Produces: `CodingAgent.run(task: str) -> RunResult`

- [ ] **Step 1: 编写完整循环、非法 JSON 修正、未知工具和最大轮数测试**

Fake Provider 依次返回 `read_file`、`edit_file`、`run_command`、`finish` 动作，断言每个工具结果都进入下一轮消息。连续非法 JSON 或超出轮数必须返回失败结果而不是抛出未处理异常。

- [ ] **Step 2: 运行 `python -m unittest tests.test_agent -v`，确认失败**

- [ ] **Step 3: 实现系统提示、严格动作解析和循环**

动作格式固定为 `{"tool": str, "arguments": object, "reason": str}`。不要求或记录隐藏思维；解析失败以工具错误消息反馈模型。`finish` 的 `summary` 成为最终结果。

- [ ] **Step 4: 重跑 Task 4 测试并确认通过**

### Task 5: CLI、文档与验收

**Files:**
- Create: `src/tricoder/cli.py`
- Create: `src/tricoder/__main__.py`
- Create: `README.md`
- Create: `.gitignore`
- Create: `.env.example`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `load_config`、`OpenAICompatibleProvider`、`CodingAgent`
- Produces: `python -m tricoder doctor ...`、`python -m tricoder run ...`

- [ ] **Step 1: 编写 CLI 参数、doctor、只读模式和退出码测试**

使用临时工作区和 patch 后的 Provider；断言缺少 Key 返回配置错误、`doctor` 不发送网络请求、审批提示只接受明确的 `y/yes`。

- [ ] **Step 2: 运行 `python -m unittest tests.test_cli -v`，确认失败**

- [ ] **Step 3: 实现 CLI 和中文 README**

README 包含架构、安装方式、环境变量占位符、三家示例、演示脚本、安全边界、测试命令、开源参考和简历描述；不得出现真实凭据。

- [ ] **Step 4: 运行完整验证**

运行：

```powershell
$env:PYTHONPATH = 'src'
python -m unittest discover -s tests -v
python -m compileall -q src tests
python -m tricoder --help
python -m tricoder doctor --provider deepseek --workspace .
```

期望：测试全通过、语法检查成功、帮助文本显示；`doctor` 仅报告 Key 是否存在且不泄露值。

- [ ] **Step 5: 自审**

检查所有需求均有测试覆盖；扫描未完成标记、疑似密钥字面量和意外英文长注释；检查项目文件列表，不触碰其他项目、不生成 Git 仓库、不安装依赖。
