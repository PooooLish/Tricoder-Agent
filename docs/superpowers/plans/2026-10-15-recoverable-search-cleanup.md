# TriCoder 可恢复搜索超时的清理期限修复方案

> **For agentic workers:** 使用 `superpowers:executing-plans` 逐项实施。本方案由用户交给独立 coding session 执行，不要求并行 Agent，不自动提交或推送。

**Goal:** 正则搜索超时且资源已清理后，允许同一任务继续；后续模型等待和正常命令不能被旧操作的清理期限误杀。

**Architecture:** 保留现有 TaskCleanup 和进程资源所有权。为固定只读搜索 helper 明确选择“可恢复超时”，让该操作的清理使用局部预算；实际取消、清理失败和已进入终止的任务仍使用原有任务级期限。

**Tech Stack:** Python 3.11+、asyncio、subprocess、unittest；不增加依赖。

**Spec:** 本文第 1～3 节；承接 `2026-10-12-liveness-verification-consistency.md` 的 A2 恢复要求，仅修复独立复审发现的 P2 问题。

## 1. 已确认问题与证据

2026-10-12 实现后的独立复审中，306 项相关现有测试通过，`git diff --check` 通过；额外探针复现以下缺陷。这些是修改前证据，不得作为本次修复后的验收结果。

```text
同一个任务、同一个 TaskCleanup
→ search_text 正则 (a+)+$ 匹配 30 个 a 加 !，触发 timeout
→ 子进程已结束，没有遗留资源，工具返回 TIMEOUT / REPLAN
→ 等待 5.2 秒，模拟模型生成下一轮响应
→ 正常执行 python -m compileall -q ok.py
→ cleanup_failed，Agent 第 3 轮提前结束
```

工具级探针观察到：搜索后 `has_pending=False`，但 `started_deadline` 已非空；后续编译返回 `cleanup_failed`。真实 CodingAgent 循环也复现了任务提前失败。Agent 内可能另有运行中的 worker lease，不能把此时整个 scope 的 has_pending 简单等同于搜索资源未清理。

根因链路：

1. `tools/search.py:SearchTextTool._search` 在进程超时、清理成功后返回可恢复错误。
2. `process/control.py:_collect_bounded_process` 却把 timed_out 统一作为 `resources.finish(..., terminal=True)` 的原因。
3. `_ProcessResources.finish` 因此调用 `TaskCleanup.begin_termination()`，启动共享的默认 5 秒期限。
4. 后续 `process/cache.py:CompileCache.finish` 正确复用了任务期限，但该期限本不该被只读搜索启动；过期后没有时间等待缓存删除线程，产生清理失败。

现有 `test_catastrophic_regex_returns_product_timeout_not_outer_kill` 在超时后立即执行下一次搜索，没有等待超过清理预算，不能覆盖这个问题。

## 2. 修改范围与约束

- 先读 `../../AGENTS.md`、项目 `AGENTS.md`、README、pyproject、project.md、原方案和本文。
- 保留当前全部未提交修改；不要从 HEAD 覆盖上一轮实现。
- 不读 `.env.local`、真实凭据或用户 sessions.db，不安装依赖、不调用真实模型、不提交或推送。
- 修改主要限定为 `process/control.py`、`tools/search.py` 及相关测试；无需重构 Agent、Runtime、Provider 或数据库。
- 保留普通命令超时的既有 UNKNOWN、停止、审批、锁与验证语义。不能把任意 run_command 超时都降级为可恢复。
- 不重置 `TaskCleanup._deadline`，不清空 failed/pending，不新建一个 scope 来隐藏旧资源，不延长任务已启动的终止期限。
- 保留 regex helper 隔离、输入输出上限、扫描预算、进程树清理、writer/reader 所有权及精确缓存目录清理。
- 不修改评测题目、fixture、hidden verifier、评分规则或验证门禁。
- 测试仅用合成文件与离线 Provider；日志放在 `runtime/recoverable-search-cleanup-20261015/`，不要覆盖旧日志。

## 3. 最小设计

### 3.1 显式内部选项

给 `process/control.py:run_bounded_process` 增加关键字参数：

```python
recoverable_timeout: bool = False
```

传递给 `_collect_bounded_process`，其同名参数也保留 False 默认值，避免破坏内部测试/兼容调用。

只有 `SearchTextTool._search` 启动固定正则 helper 时传 True。此能力不进入模型工具 schema，不由模型参数、命令文本或错误摘要决定。扫描文件在父进程完成，helper 只消费文本、执行正则，这是允许局部恢复的依据。

### 3.2 清理决策

| 场景 | 清理期限与结果 |
| --- | --- |
| helper 超时，启用 recoverable_timeout，任务尚未终止 | 使用现有操作局部清理预算；清理成功后返回 timed_out，search 映射 TIMEOUT/REPLAN；不启动任务终止期限 |
| 局部清理失败或资源结束无法确认 | 将已消费的局部截止时间交给 TaskCleanup，标记失败并保留 exact owner；返回 CLEANUP_FAILED/STOP_TASK |
| token 取消，包括超时清理期间才收到取消 | 取消优先，沿原有任务终止路径；不得因为该选项而变成可恢复错误 |
| 任务此前已经开始终止 | 必须继承已有期限，不另领局部 5 秒，不恢复任务 |
| 默认调用方、普通命令超时、输出超限、执行状态不确定异常 | 保持当前行为；本次不扩大恢复范围 |

优先复用 `_ProcessResources.finish` 已有的局部预算及失败升级机制，只调整超时是否构成任务终止原因。检查其取消令牌与已有 started_deadline 判断，不复制一套清理实现。

`BoundedProcessResult.timed_out` 仍为 True；不把超时当成功，不签发验证证据。资源清理必须仍有界，不能依赖外部测试超时结束 helper。

## 4. Review Focus

1. 同任务超时后模型等待超过 5 秒：后续搜索、编译与 finish 正常完成。
2. 超时伴随未退出子进程、writer/reader 或清理异常：仍停止、保留归属并阻止复用。
3. 取消发生在执行或局部清理中：不能返回可恢复结果。
4. 已存在更早的任务终止期限：任何局部恢复选项都不能延长期限。
5. 普通 run_command 和 Provider 行为：新参数默认不改变已有调用方，缓存/验证/评测负例继续有效。

## 5. 任务一：底层局部清理与搜索恢复

**Files:** 修改 `src/tricoder/process/control.py`、`src/tricoder/tools/search.py`；扩展 `tests/test_subprocess_control.py`、`tests/test_search_limits.py`。

**Interfaces:** `run_bounded_process(..., recoverable_timeout: bool = False)`，结果结构不变；仅固定正则 helper 显式启用。

- [x] 补失败测试 `test_recoverable_search_timeout_does_not_start_task_termination`：真实灾难性正则超时，确认 TIMEOUT/REPLAN、没有搜索遗留资源、scope.failed=False、scope.started_deadline is None。在当前实现下应因提前启动任务期限失败。
- [x] 补底层对照：相同合成只读子进程超时，选项 True 使用局部清理；省略选项时维持原有终止行为。用可控时钟/清理回调核验局部期限，不依赖极短 wall-clock 调度。
- [x] 补清理失败、已有终止期限、执行中取消和清理中取消测试。断言资源归属、期限不延长、阻止复用及原取消语义；失败清理显式释放测试资源，避免污染后续用例。
- [x] 运行新增测试，保存 RED 根因；不能把导入错误或测试自身外层超时视为正确 RED。
- [x] 按第 3 节实现最小修改；核对所有调用方，新选项只在 search helper 启用。
- [x] 运行下列聚焦测试，保存 GREEN：

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -p test_subprocess_control.py -v
.venv/Scripts/python.exe -m unittest discover -s tests -p test_search_limits.py -v
```

## 6. 任务二：同一 Agent 任务端到端回归与交付

**Files:** 新建 `tests/test_search_timeout_recovery.py`，复用现有 RecordingProvider/response 或等价合成 Provider；必要时扩展 `tests/test_command_cache.py`。更新 `project.md` 和本文实施状态；README 只需补一句准确的恢复边界。

**Interfaces:** 不新增生产接口；使用真实 CodingAgent、ToolRegistry、固定 regex helper 和 compileall。

- [x] 增加真实集成用例：预置输入文本，Agent 创建 ok.py → 灾难性正则搜索超时 → 下一轮 Provider 等待 5.2 秒 → 正常 literal 或 regex 搜索 → compileall ok.py → finish。正则预算可单独注入 0.2 秒，其他工具使用充足预算；禁用结构化记忆以免合成 Provider 接到额外摘要请求。
- [x] 明确保持同一个 Agent 任务和 TaskCleanup；禁止通过每次 ToolRegistry 独立调用产生新 scope，让测试绕过缺陷。
- [x] 验收断言：超时反馈确实进入后续模型上下文；后续命令确实执行；最终 ok=True、cleanup_failed=False、验证通过；文件内容正确，无工作区 pyc，无未回收 helper/缓存；没有重复写入或额外重试。不能只断言 provider 响应队列已消费。
- [x] 增加清理未确认的对应负例：阻止后续命令与 finish 成功，仍可追踪旧资源。已有等价测试可复用，但交付时指出覆盖位置。
- [x] 运行集成及相邻回归：

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -p test_search_timeout_recovery.py -v
.venv/Scripts/python.exe -m unittest discover -s tests -p test_command_cache.py -v
.venv/Scripts/python.exe -m unittest discover -s tests -p test_agent_async.py -v
.venv/Scripts/python.exe -m unittest discover -s tests -p test_cancellation.py -v
.venv/Scripts/python.exe -m unittest discover -s tests -p test_provider_liveness.py -v
.venv/Scripts/python.exe -m unittest discover -s tests -p test_runtime_coverage.py -v
.venv/Scripts/python.exe -m unittest discover -s tests -p test_finish_verification_recovery.py -v
.venv/Scripts/python.exe -m unittest discover -s tests -p test_eval_regression_suite.py -v
git diff --check
```

- [x] 自审 diff：只读搜索恢复没有扩张成一般命令恢复；没有吞异常、清空历史失败或更新任何评分标准。若修改了更广泛的清理语义，补跑受影响测试，并说明必要性；不为通过测试削弱原断言。
- [x] 将修改点、RED/GREEN、实际命令/测试数量、平台、限制与下一步独立复审记录到 project.md。保留失败日志，未运行的平台/真实模型不得声称通过。

## 7. 最终交付要求

交付应说明：为什么此次搜索超时可以恢复，哪些情况仍必须停止；旧共享期限不再被误启动的证据；延迟下一轮响应后的真实 Agent 回归结果；默认调用方与清理失败负例是否保持原行为。

本方案状态：2026-10-15 已实施，待独立复审。聚焦及相邻回归 110 项通过；完整项目 1744 项 / 388.403 秒，OK（13 skipped），0 failures/errors，退出 0；compileall 与 git diff --check 退出 0。完整日志保留 15 条 asyncio 慢回调诊断，跳过不计通过。

实施证据：`runtime/recoverable-search-cleanup-20261015/verification.md` 与 `progress.md`。真实搜索及同任务 5.2 秒延迟 Agent 场景已完成 RED→GREEN；底层补齐局部失败、既有期限和执行/清理期间取消负例。仅固定 regex helper 启用内部恢复参数，清理后新增 token 复查沿用已消费期限，未重置旧 deadline/failed/pending。保留原工作树与失败日志，无依赖、真实模型、数据库或提交推送操作。下一步独立复审。
