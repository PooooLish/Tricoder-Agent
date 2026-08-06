# Planner-Executor 演进设计

> 状态：已实现（见 commit 说明）。
> 目标：在不改变 Agent 循环骨架的前提下，为任务执行前增加一个**规划阶段**，
> 让模型先产出分步计划、再进入既有工具调用循环执行。

## 背景与动机

当前 `CodingAgent` 是 agentic loop：任务直接进入"请求模型 → 解析动作 → 执行工具"循环，
模型从第一个动作开始，没有显式计划。对多步编码任务，模型容易"边做边想"导致步骤漂移、
遗漏验证、或反复读文件。规划先行可以稳定任务推进顺序。

## 非目标

- 不引入独立 Planner 子代理 / 多 Agent。
- 不做探索式规划（规划阶段不调用工具，仅基于任务文本与会话摘要）。
- 不改变既有的"每轮一个工具 + 严格解析 + 审批 + 审计"安全边界。
- 不把计划文本写入 SQLite 或审计原文（维持最小化留存原则）。

## 设计

### 规划阶段（一次 provider 调用，round 0）

在 `run_with_context` 构造消息并完成审计准备后、进入轮次循环前：

1. 若 `plan_enabled=True`：以当前消息序列 + 一条 `PLANNING_PROMPT` 调用
   `provider.complete(planning_messages, tools=())` —— **不带工具**，强制模型只输出计划。
2. 模型要求返回 JSON：`{"steps": ["…", "…"]}`，3–8 步，具体可执行。
3. 解析计划（宽容降级）：
   - 先尝试 JSON `steps`；
   - 再按 Markdown 列表（`-` / 编号）解析；
   - 仍失败则把原文整体作为计划文本；空响应视为失败。
4. 计划成功 → 以 `Message("system", "执行计划：\n…")` 注入到**任务消息之后**，
   随历史进入既有工具循环，模型据此执行。
5. 计划失败（Provider 错误 / 解析失败 / 空）→ **降级为无计划执行**，不阻塞任务；
   审计记录 `plan_failed`。

### 审计与用量

- 规划阶段视为 round 0：usage 并入累计 `RunResult.usage`，并写入
  `status="provider_usage"`（round 0）与 `status="plan"` 事件；
  `on_provider_usage(0, usage)` 通知观察者。
- 计划文本**不落审计原文**：只记 `plan_chars`、步骤数、状态。
- 计划消息在 `history_start` 之后，随任务一起回滚/压缩，不进入持久化。

### 配置优先级

`plan_enabled` 默认 **True**；优先级：`--no-plan`（CLI）> 环境变量
`TRICODER_PLAN`（`"0"`/`"false"` 关闭）> 项目 TOML `[agent] plan = false` > 默认 True。
`--read-only` 与规划阶段无关（计划无副作用）。

## 消息与压缩交互

- 计划消息 `kind` 为默认 `generic`（system role），位于当前 task 消息之后，
  因此属于当前任务块；`compact_session_messages` 保留最新任务块时计划随之保留。
- `_normalize_history_task_block` 会丢弃旧任务块中非完整回合的消息，计划的 system
  消息在旧任务中不保留——符合"计划只服务当前任务"。
- `turn_result(rollback_task=True)` 丢弃 `messages[history_start:]`，计划随之回滚。

## 测试计划

- 规划被调用：FakeProvider 先收到无工具的计划请求并返回 `{"steps": [...]}`，
  断言计划注入后续消息、工具循环正常执行。
- 规划降级：规划阶段 provider 抛错 / 返回非法内容 → 仍进入工具循环执行任务。
- 关闭规划：`plan_enabled=False` 时不调用规划请求。
- 审计：plan 事件记录步骤数与字符数，不落原文。
- 配置：`TRICODER_PLAN`、`[agent] plan`、`--no-plan` 的读取优先级。

## 局限与后续

- 规划阶段不探索文件系统，计划基于任务文本与会话摘要，可能不够具体；
  后续可扩展为"规划 + 探索"（规划阶段允许只读工具）。
- 规划增加一次 API 调用与 token 成本（关闭可用 `--no-plan`）。
