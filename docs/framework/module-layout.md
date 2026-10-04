# TriCoder 模块结构与兼容入口

## 阅读顺序

Agent 的稳定入口仍是 `tricoder.agent.CodingAgent`。单任务编排位于
`tricoder.engine`，历史和结构化记忆位于 `tricoder.context`。宿主层按以下职责查找：

- `tricoder.process`：受控子进程、进程树清理、可信可执行文件和过滤后的子进程环境；
- `tricoder.workspace`：工作区互斥、验证证据、任务前后内容快照与确认门禁；
- `tricoder.session`：Session 独占、SQLite 存储和 `SessionRuntime`；
- `tricoder.presentation`：斜杠命令、审批等待、Rich 终端、Shell 和 Textual TUI。

四个包的 `__init__.py` 只说明职责，不批量导入子模块。需要存储时直接导入
`tricoder.session.store`，需要 TUI 时才导入 `tricoder.presentation.tui`。

## 迁移表

| 兼容旧入口 | 规范实现入口 |
| --- | --- |
| `tricoder.subprocess_control` | `tricoder.process.control` |
| `tricoder.subprocess_env` | `tricoder.process.env` |
| `tricoder.workspace_lock` | `tricoder.workspace.lock` |
| `tricoder.verification` | `tricoder.workspace.verification` |
| `tricoder.workspace_snapshot` | `tricoder.workspace.snapshot` |
| `tricoder.workspace_gate` | `tricoder.workspace.gate` |
| `tricoder.session_lock` | `tricoder.session.lock` |
| `tricoder.sessions` | `tricoder.session.store` |
| `tricoder.session_runtime` | `tricoder.session.runtime` |
| `tricoder.commands` | `tricoder.presentation.commands` |
| `tricoder.approval_wait` | `tricoder.presentation.approval_wait` |
| `tricoder.ui` | `tricoder.presentation.console` |
| `tricoder.shell` | `tricoder.presentation.shell` |
| `tricoder.tui` | `tricoder.presentation.tui` |

旧文件只做显式转发，不包含业务类或函数定义。常用类、函数、异常和状态对象在
新旧入口间保持对象同一性；因此不会产生第二份 ContextVar、锁注册表、缓存或其他
可变状态。旧模块的任意 monkeypatch 位置不属于普遍兼容承诺：函数迁入后会在新模块
查找全局依赖，测试和扩展应 patch 规范实现的实际查找位置。

## 依赖方向

- CLI 和 presentation 可以依赖 session、Agent 门面、workspace 与共享类型。
- session 可以依赖 Agent 门面、context、workspace 与基础组件，但不经 presentation
  获取运行能力。
- engine/tools 可以依赖 workspace/process，但不装配 SessionRuntime。
- workspace 依赖 policy、changes、core 及同包底层模块，不导入 Agent、SessionRuntime
  或 UI。既有 Windows 目录句柄绑定是受控例外：`workspace.verification` 只在
  `_bound_directory()` 的 Windows 分支延迟导入 `tools.binding`，避免模块加载期反向依赖；
  导入边界测试固定该例外不能上移到模块顶层或扩散到其他 tools 模块。
- process 只依赖取消、清理与标准库，不导入 SessionRuntime、CLI 或 TUI。

`workspace.verification.WorkspaceSnapshot` 是验证证据快照，
`workspace.snapshot.WorkspaceBaseline` 是任务门禁内容基线；名称相近但语义不同，不能合并。

## Eval 隐藏验证器

Eval 不再根据一个旧模块的目录猜测依赖位置。安装隐藏 verifier 时分别定位
`tricoder.process.control`、`tricoder.task_cleanup` 和
`tricoder.core.cancellation` 的真实源码，并复制为固定最小闭包。自动测试会清空
`PYTHONPATH`，以 `-S -B` 启动独立解释器，在存在恶意同名工作区包时实际执行成功命令
和超时命令；该 helper 不依赖开发树或机器上安装的 TriCoder。

## 后续维护

- 新内部代码只使用规范路径；旧路径仅用于兼容测试和历史调用方。
- 新增跨层导入前先运行 `test_module_import_boundaries.py` 和
  `test_agent_import_boundaries.py`。
- `SessionRuntime` 本轮只移动文件，没有拆分内部状态机。若要继续拆分，需另立计划并重新
  固定锁、取消、记忆异常进度和资源所有权语义。
- setuptools 会发现四个新包；本地分发产物仍须在具备兼容构建工具的环境中验证。
