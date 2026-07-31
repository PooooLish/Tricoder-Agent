# TriCoder Rich CLI UI 设计

## 目标

将现有纯文本 CLI 改造成清晰、专业、适合简历演示的终端界面，同时保持 Agent 核心、安全审批、审计日志和非交互环境行为稳定。

## 视觉层级

- 青色用于产品标题和当前步骤。
- 绿色用于成功状态。
- 黄色用于审批与风险提示。
- 红色用于错误和拒绝。
- 灰色用于路径、耗时和其他次要信息。
- 非 TTY 或 `--no-color` 模式关闭颜色和动画，但保留完整文字。

## 组件

- 启动面板：任务、Provider、模型、工作区和执行模式。
- 轮次状态：模型请求期间显示 spinner 和当前轮数。
- 工具事件：显示工具名称、公开操作理由、结果状态、字符数和耗时。
- 审批面板：文件修改使用 diff 高亮，命令显示目录、命令与超时。
- Doctor 表格：显示 Provider、模型、Base URL、Key 状态与网络状态。
- 完成面板：显示状态、摘要、轮数和审计轨迹。
- 错误面板：配置、Provider 和任务错误使用一致格式。

## 架构

新增 `ui.py`，由 `TerminalUI` 负责 Rich 渲染和审批输入。Agent 通过可选的 `AgentObserver` 接口报告轮次、动作和工具结果，默认使用空观察者，避免核心逻辑依赖终端。

审计日志继续保存纯 JSONL 元数据，不包含 ANSI 控制符、spinner 状态或 Rich 标记。

## 测试

使用 Rich `Console(record=True, width=100)` 捕获稳定文本，覆盖启动、doctor、diff 审批、错误、完成和 `--no-color`。Fake Observer 验证 Agent 事件顺序；原有安全与 Provider 测试保持不变。
