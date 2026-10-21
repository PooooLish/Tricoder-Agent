# Fixed Regression V1

该套件把近期真实故障固化为 12 个公开合成场景。六个 `quality` 题用于真实
Provider 质量评测，六个 `contract` 题由仓库内固定脚本 Provider 驱动，用于离线验证
宿主契约。两类结果分别汇总；`fake` 结果不能计入模型质量成绩。

## 评分矩阵

| 场景 | 类型 | 期望 | 主要评分证据 |
| --- | --- | --- | --- |
| `q-nested-create` | quality | completed | 隐藏测试验证嵌套文件、精确输出和修改范围；Agent 正常 finish |
| `q-review-failing-tests` | quality | completed | 观察到真实失败检查；报告包含文件、失败用例和根因；零修改 |
| `q-history-recap` | quality | completed | 第二轮请求实际发生；回顾包含目标文件、只读审查和零写入事实 |
| `q-spill-tail` | quality | completed | 真实结果发生 spill，随后调用 `read_tool_result`；报告包含尾部标识和原因 |
| `q-fix-regression` | quality | completed | 隐藏测试同时验证新行为、旧公开接口、修改范围和 finish |
| `q-memory-restart` | quality | completed | 记忆经预览保存；Runtime 实例改变而 Session ID 不变；恢复请求带记忆且产物正确 |
| `c-text-termination` | contract | blocked | 三次原生无工具响应后按专用原因停止；保留 `agent_completed=false` |
| `c-repeat-failure` | contract | blocked | 相同读取错误触发重复失败收敛，不以 `max_rounds` 冒充 |
| `c-approval-denied` | contract | blocked | 写入审批确实被拒，之后没有成功写入；保留 Agent 未完成事实 |
| `c-restart-unchanged` | contract | completed | 同一 Session 读取持久基线，无重复确认，Provider 正常进入 |
| `c-restart-changed` | contract | mixed | 接受与拒绝使用独立副本；逐项核验通知、确认、revision、Provider 调用和原始完成状态 |
| `c-baseline-corrupted` | contract | blocked | 数组、对象、非法 JSON 三个独立子场景均阻断 Provider，原记录不被覆盖 |

隐藏 verifier 只负责产物或合成 fixture 事实。回读、审批、恢复、通知、停止原因等
必须来自宿主观察；模型总结不能自行创建可信行为证据。预期安全停止可以令
`expectation_met=true`，但不会把原始 `agent_failed` 或 `agent_completed=false` 改写为成功。

## 离线验证

以下命令不读取 Provider Key，也不发送模型请求：

```powershell
.\.venv\Scripts\python.exe -m tricoder eval evals/regression-v1 --dry-run --no-color
.\.venv\Scripts\python.exe -m tricoder eval --experiment evals/regression-v1/experiment-quality.toml --dry-run --no-color
.\.venv\Scripts\python.exe -m tricoder eval --experiment evals/regression-v1/experiment-contract.toml --dry-run --no-color
.\.venv\Scripts\python.exe -m tricoder eval --experiment evals/regression-v1/experiment-contract.toml --no-color
```

最后一条运行 6 个 contract trial，并在 `runtime/evals/<run-id>/` 写入结构化结果和
Markdown 报告。它使用真实 Agent、SessionRuntime、临时 SQLite、工作区门禁和工具，
只替换模型决策；因此是工程契约证据，不是模型效果分数。

## 真实 quality 运行

单题试跑应使用本地 quality 实验清单，以保留行为断言和真实记忆配置。模板固定每题
3 次，因此以下命令只运行所选题的 3 个独立 trial：

```powershell
.\.venv\Scripts\python.exe -m tricoder eval --experiment evals/regression-v1/experiment-quality.local.toml --case q-nested-create --no-color
```

完整 baseline 固定为 6 题 × 3 次。模板故意使用
`replace-with-explicit-model`，未替换时真实运行会在 Provider 构造前拒绝。请先在同目录
复制一份本地清单，只替换模型 ID，再执行：

```powershell
Copy-Item evals/regression-v1/experiment-quality.toml evals/regression-v1/experiment-quality.local.toml
# 编辑 experiment-quality.local.toml，将 replace-with-explicit-model 换成完整模型 ID。
.\.venv\Scripts\python.exe -m tricoder eval --experiment evals/regression-v1/experiment-quality.local.toml --case q-nested-create --no-color
# 确认单题结果和费用后，再运行完整 18 次 baseline：
.\.venv\Scripts\python.exe -m tricoder eval --experiment evals/regression-v1/experiment-quality.local.toml --no-color
```

运行前应确认 Provider、完整模型 ID、18 次预算和费用。基线应记录 commit、dirty diff、
题库/评分指纹和环境。代码对照只允许代码变化时，使用：

```powershell
.\.venv\Scripts\python.exe -m tricoder eval-compare <baseline-result.json> <candidate-result.json> --allow-variable code --no-color
```

当前套件是公开回归集，不代表 holdout 泛化；工作副本隔离也不是操作系统沙箱。
