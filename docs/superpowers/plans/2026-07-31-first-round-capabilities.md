# TriCoder First-Round Capabilities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 TriCoder 能安全创建文件、可靠判断任务完成状态、把审计日志移出目标仓库，并限制模型消息历史大小。

**Architecture:** 保持现有 `CLI → CodingAgent → ToolRegistry` 边界。工具层负责安全写入，Agent 层负责验证状态和消息压缩，配置层负责可覆盖的资源预算与审计目录，UI 只展示最终公开状态。

**Tech Stack:** Python 3.11+、标准库、Rich 15.x、`unittest`

## Global Constraints

- 所有新增或修改的代码使用规范、容易理解的中文注释。
- 不读取、打印、复制或写入真实 `.env.local` 和任何真实 API Key。
- 不发起真实网络请求。
- 不初始化 Git、不提交、不安装新依赖。
- 每个生产代码改动必须先有能够按预期失败的测试。
- 保持现有工作区隔离、敏感路径拒绝、人工审批和原子文件写入语义。

---

### Task 1: 安全创建文件

**Files:**
- Modify: `src/tricoder/tools.py`
- Modify: `src/tricoder/agent.py`
- Test: `tests/test_tools.py`
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `WorkspacePolicy.resolve_path(path, must_exist=False)`、`ToolContext.approver`
- Produces: 工具名 `create_file`，参数 `{"path": str, "content": str}`

- [ ] **Step 1: 写失败测试**

在 `tests/test_tools.py` 增加真实临时目录测试：批准后创建 UTF-8 文件；拒绝时不创建；已存在目标不覆盖；敏感路径被拒绝。

- [ ] **Step 2: 运行测试并确认因未知工具失败**

Run: `python -B -m unittest tests.test_tools.ToolTests.test_create_file_requires_approval_and_writes_new_file -v`

- [ ] **Step 3: 写最小实现**

在注册表和系统提示中注册 `create_file`。使用统一 Diff 展示新增内容，批准后写完整的同目录临时文件，并用 `os.link` 原子、不可覆盖地发布；硬链接不可用、父目录不存在或目标已存在时安全拒绝。

- [ ] **Step 4: 增加 Agent 统计测试**

验证成功创建的路径进入 `RunResult.modified_files`，且审计参数只记录路径和内容字符数。

- [ ] **Step 5: 运行聚焦测试**

Run: `python -B -m unittest tests.test_tools tests.test_agent -v`

### Task 2: 可靠完成判定

**Files:**
- Modify: `src/tricoder/agent.py`
- Test: `tests/test_agent.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `ToolResult.ok`、工具名 `create_file|edit_file|run_command|finish`
- Produces: `RunResult.ok` 和公开验证状态 `未运行|待验证|通过|失败`

- [ ] **Step 1: 写失败测试**

增加五个行为测试：无修改直接结束成功；修改未验证返回失败；验证失败后结束返回失败；修改后验证成功返回成功；验证成功后再次修改返回失败。

- [ ] **Step 2: 运行测试并确认旧逻辑错误地返回成功**

Run: `python -B -m unittest tests.test_agent -v`

- [ ] **Step 3: 写最小状态转换**

成功文件写入设为 `待验证`；每次命令结果设为 `通过` 或 `失败`；`finish` 根据是否发生文件修改和当前验证状态决定 `RunResult.ok`，未验证时在摘要中说明原因。

- [ ] **Step 4: 验证 CLI 退出码**

增加 CLI 测试，确认“修改后未验证”的任务返回退出码 `1`。

- [ ] **Step 5: 运行聚焦测试**

Run: `python -B -m unittest tests.test_agent tests.test_cli -v`

### Task 3: 审计日志迁出目标工作区

**Files:**
- Modify: `src/tricoder/config.py`
- Modify: `src/tricoder/models.py`
- Modify: `src/tricoder/cli.py`
- Test: `tests/test_config.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `default_audit_dir(environ: Mapping[str, str] | None = None) -> Path`
- Produces: CLI 选项 `--audit-dir PATH`
- Produces: `AppConfig.audit_dir: Path | None = None`，`load_config` 返回的运行配置始终填入已解析的绝对路径

- [ ] **Step 1: 写默认目录失败测试**

用受控环境验证 Windows `LOCALAPPDATA` 和非 Windows `XDG_STATE_HOME` 分支返回本地状态目录，不依赖真实用户环境。

- [ ] **Step 2: 写 CLI 行为失败测试**

显式传入临时 `--audit-dir`，运行 Fake Provider 后确认日志只出现在指定目录，目标工作区没有 `runtime/`。

- [ ] **Step 3: 实现配置与 CLI**

配置加载接受 `audit_dir`，CLI 增加参数并将运行文件写到 `config.audit_dir`。显式相对路径按当前进程目录解析，不按目标工作区解析。

- [ ] **Step 4: 运行聚焦测试**

Run: `python -B -m unittest tests.test_config tests.test_cli tests.test_audit -v`

### Task 4: 有界消息上下文

**Files:**
- Modify: `src/tricoder/models.py`
- Modify: `src/tricoder/config.py`
- Modify: `src/tricoder/cli.py`
- Modify: `src/tricoder/agent.py`
- Test: `tests/test_config.py`
- Test: `tests/test_agent.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `AppConfig.max_context_chars: int = 80_000`
- Produces: `compact_messages(messages: list[Message], max_chars: int) -> list[Message]`
- Produces: CLI 选项 `--max-context-chars`

- [ ] **Step 1: 写压缩函数失败测试**

使用固定短消息证明：预算充足时保持原序列；超预算时保留前两条固定消息和最新消息；插入固定压缩说明；输入列表不被修改。

- [ ] **Step 2: 写配置优先级失败测试**

验证 CLI 值覆盖 `TRICODER_MAX_CONTEXT_CHARS`，环境变量覆盖 `[agent].max_context_chars`，默认值为 `80_000`，非正整数被拒绝。

- [ ] **Step 3: 实现最小压缩逻辑**

每轮调用 Provider 前传入压缩后的新列表。按 `len(role) + len(content)` 计算字符预算，从最新消息反向保留；固定消息不截断。

- [ ] **Step 4: 接入 CLI 和 Agent**

`CodingAgent` 构造函数验证正预算，CLI 将配置值传入 Agent。

- [ ] **Step 5: 运行聚焦测试**

Run: `python -B -m unittest tests.test_config tests.test_agent tests.test_cli -v`

### Task 5: 集成验证与文档同步

**Files:**
- Modify: `README.md`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: Tasks 1–4 的最终公开 CLI 和行为

- [ ] **Step 1: 更新文档**

记录 `create_file`、完成判定、`--audit-dir`、`--max-context-chars` 和审计默认位置；说明修改未验证时退出码为 `1`。

- [ ] **Step 2: 忽略工作流临时状态**

将 `.superpowers/` 加入 `.gitignore`，避免视觉原型和子 Agent 工作记录进入未来的独立仓库。

- [ ] **Step 3: 运行完整离线验证**

Run: `python -B -m unittest discover -s tests -v`

Run: `python -B -m compileall -q src tests`

- [ ] **Step 4: 安全自检**

仅扫描受版本控制候选源码和文档，并明确排除 `.env.local`、`.superpowers/`、`runtime/`，确认没有真实凭据字样或敏感内容。
