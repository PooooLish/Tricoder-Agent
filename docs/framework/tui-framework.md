# TUI Framework: Textual

## Decision

TriCoder 的本地交互 TUI 基于 **Textual**（`textual>=8.0.0,<9`），与现有 rich
渲染栈同源。新增 `src/tricoder/tui.py` 作为 `tricoder tui` 入口。

## Why not alternatives

- 增强现有 rich `TerminalUI`：rich 是渲染库，没有输入、布局、事件循环与模态
  能力；自己实现 TUI 框架成本高、天花板低。
- `curses`（stdlib）：跨平台体验差、无组件化，Windows 支持弱。
- HTTP API + 浏览器/远程：超出"本地交互"目标；API 层可作为后续演进，
  当前 SessionRuntime/CodingAgent 已是可编程库接口。

## Dependency review (2026-08-05)

| Area | Evidence |
| --- | --- |
| Need | 组件化 TUI（消息流、输入、审批模态、异步 worker）；rich 无法独立满足 |
| Identity | `textual` 8.2.8 · Textualize · github.com/Textualize/textual · PyPI · 2026-06-30 发布 |
| License | MIT（GitHub LICENSE，Copyright (c) 2021 Will McGugan），允许商用/再分发 |
| Security | GitHub security-advisories 无已发布公告（查询于 2026-08-05） |
| Maintenance | 活跃维护，8.x 持续发布；Python >=3.9,<4.0 |
| Runtime | 纯 Python wheel；依赖 rich>=14.2.0（本项目 rich>=15 满足）、platformdirs、pygments、markdown-it-py 等，均纯 Python；无 install hooks、无原生二进制、无网络行为 |
| Change | `pyproject.toml` 增加 `textual>=8.0.0,<9`；传递依赖 6 个（linkify-it-py、mdit-py-plugins、platformdirs、typing-extensions、uc-micro-py、mdurl 等） |
| Decision | **approve**；条件：固定在 `<9` 范围内，锁文件随 CI 复核 |

## Architecture

- `TricoderApp`：Textual App，`Header` + `RichLog`（事件流）+ `Input`（任务/命令）
  + `Footer`；`Ctrl+Q` 退出（先 `retry_persist`），`Ctrl+C` 清空输入。
- `TuiObserver`：`AgentObserver` 实现，后台线程事件经 `call_from_thread`
  转发到 UI 线程。
- `ApprovalScreen`：模态审批，`y` 允许 / `n`、`Esc` 拒绝；worker 线程经
  `threading.Event` 阻塞等待用户响应。
- 斜杠命令复用 `parse_command`；跨工作区会话列表/切换暂不提供（提示回交互 CLI）。
- **安全边界完全复用** `SessionRuntime` / `CommandPolicy` / `ToolRegistry`：
  写操作与命令执行仍在模态中明确确认；`--read-only` 由运行配置生效。

## Threading model

Agent 循环（同步、含 Provider 网络与工具执行）在 `run_worker(thread=True)`
后台线程运行；UI 事件通过 `call_from_thread` 更新 `RichLog`；审批通过
`push_screen_wait` + `threading.Event` 桥接 UI 与 worker 线程。
Textual 8.x 的 `run_worker` 不接受位置参数，传参需用闭包。

## Testing

`tests/test_tui.py` 用 `app.run_test()` + pilot：启动、任务流（FakeProvider
返回 finish）、审批模态拒绝写入。测试 workspace 使用临时子目录、`sessions.db`
置于 workspace 之外（SessionStore 拒绝数据库位于工作区内）。

## Limitations

- `/session` 列表与跨工作区切换未实现（MVP）。
- 大输出（命令 stdout 等）在 `RichLog` 中整行显示，未做分页/折叠。
- worker 取消（任务运行中退出）未处理；退出时任务线程可能仍在跑。
