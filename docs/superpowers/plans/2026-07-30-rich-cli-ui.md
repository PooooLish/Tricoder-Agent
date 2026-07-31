# TriCoder Rich CLI UI Implementation Plan

**Goal:** 使用 Rich 提升 CLI 的层级、状态反馈、审批体验和完成摘要。

**Architecture:** `ui.py` 独立封装所有 Rich 组件，`agent.py` 通过观察者发送无渲染语义的事件，`cli.py` 负责组装。核心工具与审计层不依赖 Rich。

**Tech Stack:** Python、Rich 15.x、argparse、unittest

**Implementation Status:** Completed locally on 2026-07-30 with Rich 15.0.0. No Git initialization, commit, push, or publish action was performed.

## Tasks

1. 先编写 UI 录制输出测试，再实现 `TerminalUI` 的启动、doctor、错误、审批和完成视图。
2. 先编写 Agent 观察者事件测试，再实现轮次、动作、结果和完成事件。
3. 先更新 CLI 测试，再集成 `TerminalUI` 和 `--no-color`。
4. 更新依赖、开源评估和 README，安装 Rich 15.x。
5. 运行全量测试、语法检查、CLI 快照演示、凭据扫描和自审。

## Constraints

- 审批仍只接受 `y/yes`。
- 非 TTY 和 `--no-color` 必须可读。
- 不把 Rich 标记或 ANSI 控制符写入审计日志。
- 不新增 Rich 之外的运行依赖。
- 不修改或输出 `.env.local` 中的真实密钥。
