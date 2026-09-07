# Hcode 迁移 Phase 1 验证记录

验证日期：2026-09-03

范围：仅建立 Provider 无关的类型化事件、线程安全取消令牌和原子执行预算契约；
不修改 `CodingAgent`、Provider、Session、CLI 或 TUI 的现有运行行为。

## 实现边界

- `src/tricoder/core/events.py`：不可变的 Provider/Agent 事件联合类型和
  `EventSink`。自由文本、工具参数及结果不进入事件 `repr`。
- `src/tricoder/core/cancellation.py`：幂等、线程安全、父向子单向传播的
  `CancellationToken`，并以 `CancellationError` 区分主动取消。
- `src/tricoder/core/budgets.py`：轮次、token、单调时钟截止时间和子任务数预算；
  多维消费在同一把锁内先检查后扣减，拒绝时不发生部分消费。
- `src/tricoder/core/__init__.py`：核心类型的稳定入口。

所有实现只使用 Python 标准库和 TriCoder 自身的数据模型；没有导入 TUI、Session、
Tools、Provider 具体实现、Provider SDK 或 Hcode 包。Hcode 仅作为行为和测试场景参考，
本阶段没有复制其实现代码。

## TDD 证据

RED 命令：

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_core_events tests.test_cancellation tests.test_budgets -v
```

实现前退出码为 1，三个测试模块均因 `ModuleNotFoundError: No module named
'tricoder.core'` 失败，原因与预期一致。

GREEN 使用相同命令，退出码为 0，`Ran 12 tests`，结果为 `OK`。测试覆盖：

- 文本、thinking、工具生成与执行、usage、规划、审批、压缩、子 Agent、失败和完成事件；
- frozen 事件及敏感动态字段的 `repr` 隐藏；
- 初始取消状态、幂等取消、跨线程触发和父子单向传播；
- 轮次、token、时间、子任务的边界、拒绝、原子多维扣减和并发非负保证。

## 完整验证

| 命令 | 退出码 | 结果 |
| --- | ---: | --- |
| `.\.venv\Scripts\python.exe -B -m unittest discover -s tests` | 0 | `Ran 557 tests in 76.192s`; OK，4 skipped |
| `.\.venv\Scripts\python.exe -B -m compileall -q src tests` | 0 | 通过 |
| `.\.venv\Scripts\python.exe -B -m tricoder eval evals\smoke --dry-run --no-color` | 0 | 3/3 cases validated |
| 独立进程导入 `tricoder.core` 并检查禁用模块 | 0 | 未加载 Hcode、TUI、Session、Tools 或 Provider 具体模块 |

4 个 skip 与既有 Windows 符号链接权限边界一致；本阶段没有新增 skip。Textual 的慢任务
诊断没有导致测试失败。工作区 doctor 在文档状态字段改为规范枚举前发现 1 项格式问题；
根因是状态值附带阶段说明，已将 `Status` 恢复为精确的 `active`，阶段说明保留在进度中；
修正后 doctor 退出码为 0，报告 0 项问题。

## 限制与下一步

- 新事件、取消和预算目前是稳定基础契约，尚未接入现有同步 Agent 循环。
- Provider 真流式输出、流中断处理和端到端取消属于 Phase 2。
- token 上下文管理与大工具结果落盘属于 Phase 3。
- 未验证 Linux/macOS、Python 3.12 或真实 Provider；它们不是本阶段完成门槛。
- 没有新增、升级或安装第三方依赖，也没有提交或推送代码。
