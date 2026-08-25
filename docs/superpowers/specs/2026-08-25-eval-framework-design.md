# TriCoder Eval 框架设计

## 目标

为 TriCoder 增加一个本地、可重复、默认调用真实 Provider 的 Coding Agent
评测框架。框架使用确定性验证规则评估任务完成情况，记录成功率、耗时、Agent
轮数、工具调用数和 Token 用量，并为 OpenAI、DeepSeek、GLM 提供一致入口。

首版不引入 LLM Judge、排行榜、并行执行或重复采样。离线能力用于校验评测定义
和自动测试框架本身，不把假 Provider 的结果包装成真实 Agent 能力指标。

## 方案选择

采用混合式架构：Eval 核心在当前进程中运行，以便直接取得结构化
`RunResult`；Provider、配置、安全策略、工具注册表和 Agent 构建逻辑复用生产
代码。公开入口为 `tricoder eval`，默认使用真实 OpenAI Provider，也允许显式
选择 DeepSeek 或 GLM。

不采用纯 subprocess 黑盒方案，因为现有 `tricoder run` 没有稳定的机器可读输出，
且无人值守审批难以安全表达。不采用完全独立的进程内实现，因为复制生产装配逻辑
会使 Eval 与真实 CLI 逐渐偏离。

## 目录结构

```text
evals/<suite>/
├── suite.toml
└── cases/
    └── <case-id>/
        ├── case.toml
        └── workspace/

src/tricoder/evals/
├── __init__.py
├── models.py
├── loader.py
├── runner.py
└── report.py

runtime/evals/<run-id>/
├── workspaces/
├── result.json
└── report.md
```

评测定义和 fixture 是版本控制内的只读输入。所有 Agent 修改、验证过程和生成报告
都位于 `runtime/evals/`，不得改动原 fixture。

## 任务定义

每个 `case.toml` 包含：

```toml
id = "fix-subtract"
title = "修复减法函数"
task = "修复 calculator.py 中 subtract 的错误，并运行测试。"

allowed_changes = ["calculator.py", "tests/**"]
required_changes = ["calculator.py"]
max_rounds = 8
max_context_chars = 40000

[[verification]]
name = "unit-tests"
command = "python -m unittest discover -s tests -q"
timeout = 30
```

`suite.toml` 声明稳定的 suite ID、标题和 case 顺序。loader 必须在任何 Provider
调用之前完成全套校验，包括：ID 唯一性、目录边界、字段类型、正数限制、fixture
存在性、相对 glob，以及验证命令能否通过 `CommandPolicy`。

## 执行流程

1. CLI 加载 suite，并在启动 Provider 前完整校验所有 case。
2. 为本次运行创建唯一的 `runtime/evals/<run-id>/`。
3. 每个 case 将 `workspace/` 复制到独立工作副本。
4. 对副本建立修改前文件快照。
5. 复用生产装配路径构建真实 Provider、`CodingAgent`、`WorkspacePolicy`、
   `CommandPolicy` 和 `ToolRegistry`。
6. Eval 工作副本采用无人值守 fullaccess 审批；工具仍受只读开关、命令白名单、
   敏感路径和工作区边界限制。
7. Agent 返回后重新建立文件快照，计算规范化相对修改路径。
8. 在相同工作副本中执行 case 的最终验证命令。
9. 计算确定性判定，记录结构化结果，并继续运行后续 case。
10. 原子生成 `result.json` 与 `report.md`。

单个任务的失败或异常不终止 suite；无法加载 suite、无法创建输出目录或 Provider
配置无效属于运行级错误，在执行任何 case 前终止。

## 判定与指标

case 只有在以下条件全部满足时为 `passed`：

- `RunResult.ok` 为真；
- Agent 最终验证状态为通过；
- 所有最终验证命令退出码为零；
- 实际修改路径全部匹配 `allowed_changes`；
- 每个 `required_changes` 模式至少匹配一个实际修改路径；
- 没有框架级异常。

正常执行但条件不满足为 `failed`；Provider、装配、复制、快照或验证执行出现受控
异常为 `error`。失败原因使用固定分类与简短安全信息，不持久化 Provider 原文、
源码、补丁或完整命令输出。

每个 case 记录 Provider、模型、状态、耗时、轮数、工具调用数、Token 用量、规范化
修改路径、验证名称和退出码。suite 汇总总数、通过率、总耗时和可计算的 Token
合计；未知用量保持未知，不能伪装为零。

## CLI 与退出码

```powershell
tricoder eval evals/smoke
tricoder eval evals/smoke --provider deepseek
tricoder eval evals/smoke --case fix-subtract
tricoder eval evals/smoke --dry-run
```

- 默认模式调用真实 Provider，默认 Provider 沿用 CLI 的 OpenAI 默认值。
- `--case` 只运行一个已定义 case，便于控制费用。
- `--dry-run` 只校验 suite、fixture、路径模式和验证命令，不加载 Key、不构建
  Provider、不创建任务工作副本。
- 退出码 `0`：全部选定 case 通过，或 dry-run 校验成功。
- 退出码 `1`：至少一个 case 为 failed/error。
- 退出码 `2`：CLI、suite、配置或运行目录准备失败，未形成有效评测运行。

## 安全与隔离

- Eval 的 fullaccess 是 TriCoder 审批级别，不是操作系统沙盒，公开文档必须明确。
- 工作副本必须解析在本次 run 目录内，复制和报告写入不能接受越界路径。
- 验证命令使用 `CommandPolicy` 解析后的 argv，不通过 shell 执行。
- 子进程沿用现有敏感环境变量过滤、超时、输出上限和 Git 仓库边界。
- 报告不得保存 API Key、环境变量、Provider 异常原文、源码或补丁正文。
- Eval 不读取或显示 `.env.local`；真实模式仅复用现有配置加载边界。

## MVP 用例

首个 `evals/smoke` suite 提供三个小型、彼此隔离的 Python 任务：

1. `fix-subtract`：修复单文件逻辑错误。
2. `add-validation`：实现参数校验并满足已有测试。
3. `cross-file-feature`：修改实现并补充测试，验证跨文件能力。

fixture 只包含合成代码和测试，不包含真实项目数据、密钥或私有信息。

## 测试策略

- loader 单元测试覆盖合法输入、重复 ID、越界目录、非法 glob、缺失 fixture、危险
  验证命令和 case 过滤。
- runner 测试通过注入式假 Agent runner 验证副本隔离、路径快照、评分、单 case
  错误继续执行、Token 聚合和稳定退出语义。
- report 测试验证 JSON schema、Markdown 摘要、未知 Token 和敏感自由文本不落盘。
- CLI 测试验证默认真实模式、Provider 选择、`--case`、`--dry-run` 以及退出码。
- 全量自动测试不调用网络；真实 Provider Eval 只由用户显式运行命令触发。

## 非目标与后续扩展

MVP 不提供 OS 级沙盒、并行任务、统计重复采样、模型裁判、基准排行榜、远程任务
下载或跨 Provider 批量矩阵。后续可在不改变 case 定义核心语义的前提下增加
`--repeat`、Provider 矩阵、Docker/WSL 隔离后端和历史趋势报告。
