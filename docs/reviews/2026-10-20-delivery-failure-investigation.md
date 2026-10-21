# 真实质量失败：交付流程独立核查与修复依据

2026-10-20。独立 reviewer；用户授权核查并编写修复文档，生产实施交给 coding session。本轮无生产代码、现有题库、默认权限或历史成绩修改，无真实 Provider 调用。

## 结论与对前序推测的修正

主要确定问题在 Agent 的验证反馈及诊断；评测前置消息需要更明确，但没有证据支持放宽完整通过标准。默认权限不是本轮瓶颈。

1. **F1 / P2：反馈会错误宣布验证义务已满足。** 存在旧失败/过期检查时，另一条成功检查足以使反馈返回 satisfied / verification_accepted，随后 finish 的严格门禁仍拒绝。应修正反馈的一致性，不能清空旧失败以取得通过。
2. **F2 / P3：失败任务的收尾提示错误归因为“未归属变化”。** 独立确认变化 attributable=true 的正常修改任务，仅因预算耗尽仍得到这个提示。应细分原因，保留原有停止和下次确认要求。
3. **F3 / P2（诊断缺口）：现有证据无法区分关键结束路径。** finish 审计只保存 summary_chars；逐轮 Eval 不保存 finish 声明、验证报告状态和未解决检查数。不能从 tool status=ok 推断完成声明被误拒绝。
4. **F4 / 有界工具恢复改进：create_file 的前置“文件已存在”终止整个任务。** 在自动批准器下仍被拒绝，而且在进入审批前已经停止；可将这一无副作用的使用错误单独改为可恢复。不能由此推断历史两次 policy_denied 必然来自该分支。
5. **未确认门禁误判。** 根据历史工具顺序重建的离线例子可复现第 7 轮 finish 后待验证，但它含未解决的旧检查；补跑相同失败检查后第 8 轮通过。历史审计缺少命令完整参数和 finish outcome，因此是机制复现，不是逐字重放或历史根因的唯一证明。
6. **前置确认无需修改或测试即可成功。** `finish(completed)` 对纯“初始决定使用 red”消息可在 1 轮成功；后续修复也能成功。此前“前置消息被要求像编码任务一样验证”的担忧不成立。AND 评分真实保留前轮失败，不应改成只看末轮或忽略 needs_input。
7. **预算确会截断正常结束，但真实收益未知。** 同一合成 9 轮序列，max_rounds=8 在 finish 前停止，16 在第 9 轮成功；此结果不代表真实模型把 8 改 16 就能通过。

## 证据与复现

证据目录：`runtime/delivery-diagnosis-20261020/`。主入口 `verification.json`、`probe.py`、`followup.py`、`boundaries.py`、`verify.py`；主验证场景为 `attempt3/`。

独立测试命令（通过已有环境，不安装依赖）：

```powershell
.venv/Scripts/python.exe -X utf8 -B -c 'import unittest,sys; sys.path.insert(0,"tests"); names=["test_finish_coverage","test_finish_verification_recovery","test_task_validation","test_eval_diagnostics","test_eval_scenarios"]; suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(n) for n in names); result=unittest.TextTestRunner(verbosity=2).run(suite); sys.exit(not result.wasSuccessful())'
```

结果：**78 tests / 31.574 秒 / 0 failures、errors、skips / exit0**。日志 focused-tests.log。这些既有测试通过，并未覆盖下面的反馈矛盾。

| 离线场景 | 结果 |
| --- | --- |
| ack_only | 不修改、不运行命令，1 轮 finish 成功 |
| ack_then_work | 前置确认 1 轮成功，实际修复 3 轮成功，整体成功 |
| ask_then_work | 前轮 needs_input，后轮修复成功；整体仍失败，符合 AND |
| reconstructed_completed | 第 7 轮 completed；先收到 satisfied，最终 stale、有未解决检查 |
| reconstructed_incomplete | 第 7 轮 incomplete；同样 tool status=ok、最终未完成 |
| reconstructed_compile | 增加 compileall 仍未解决另一条失败检查，8 轮未完成 |
| targeted_tests_then_compile | 定向 tests 未覆盖源码，补 compileall 后 6 轮成功 |
| budget_8 | 含旧失败的 8 轮序列仍失败，增加时间本身不能替代修复 |
| retry_exact_failed_check | 重跑原失败的 discovery 命令后第 8 轮成功 |
| same_sequence_budget8 | 第 8 轮停止，尚有第 9 轮 finish 未消费 |
| same_sequence_budget16 | 相同序列第 9 轮成功 |
| budget_attribution | attributable=true，但预算停止摘要错误称“未归属变化” |
| create-existing | 0 次审批，policy_denied/stop_task，原文件字节不变 |

探针使用本地合成 Provider，通过真实 `_execute_case → SessionRuntime → CodingAgent → ToolRegistry` 及受控 Python 子进程，不连接模型。现有评测工作区生产文件和测试只作为合成夹具读取；不读取历史会话数据库或真实凭据。probe.log 是首轮探针配置错误：fake 会触发内置脚本 Provider 查找，未注册案例导致初始化失败；随后改为注入合成 Provider。attempt2 为初步运行，attempt3 显式对齐外层 PYTHONDONTWRITEBYTECODE=1；受控命令自身也设置该变量，不将前两次运行计为额外产品发现。全部日志保留。

verify.py 对 13 场景关键结果断言通过；479 个源码/测试/题库 .py/.toml 与运行前一致，上一轮真实评测的 41 份原生 JSON/JSONL/Markdown hash 未变。没有重跑项目全量测试或真实 8/16 轮比较。

## F1：验证反馈与完成门禁不一致

位置：`src/tricoder/engine/verification_feedback.py:_obligation`、`_reason_code`；对照 `engine/completion.py:assess_finish`、`engine/validation.py:TaskValidationTracker`。

合成序列：修改 config.py → 创建 tests/test_config.py → 根目录 unittest（0 tests）→ `unittest discover -s tests -t . -v`（tests 非 package，失败）→ 创建 tests/__init__.py → 根目录 unittest 成功 → finish。

最后一条命令成功并覆盖根目录，模型实际收到：

```json
{"verification_obligation":"satisfied","pending_paths":[],"reason_code":"verification_accepted"}
```

但 tracker 的另一个命令签名仍有旧失败/过期记录。`_obligation` 只看 state.evidence、state.verification 和覆盖范围，没有检查 tracker.status/unresolved；`assess_finish` 会检查 stale。补运行先前失败的同一 discovery 命令后成功，证明门禁无需放宽。

要求：从当前宿主事实计算统一的有界诊断；存在未解决或过期检查时不能声称 satisfied；反馈区分零测试、覆盖不足、旧失败、旧记录过期。保留旧检查同签名替代、当前快照/authority、取消及清理门禁。反馈不得成为授权或验证能力。

## F2：收尾错误标签

位置：`src/tricoder/session/runtime.py:_finalize_workspace_task`，约 1633–1653。当前成功推进基线需要 result.ok 等条件；所有未通过该条件的变化任务最后都追加“未归属的工作区变化”。

独立包装 `task_changes_match_baselines` 仅记录返回值：简单 edit→compileall→预算耗尽时返回 true，仍显示此标签。原因是任务没有完成，不是文件归属无法确认。要求只修正归因/诊断文案；不自动推进失败任务基线、不撤销下次确认、不恢复证据。

## F3：缺少必要的结束证据

位置：`src/tricoder/engine/telemetry.py:summarize_arguments`（finish 仅 summary_chars）、`evals/models.py:EvalTurnRecord`、`evals/service.py:_ExecutionObservation.on_user_turn_completed`。

需要保存白名单字段：成功解析的 finish 声明（completed/incomplete，旧数据 unknown）、最终 turn 的验证报告状态、未解决检查数、当前验证义务。按真实终态记录预算停止/模型声明未完成/验证阻塞/运行时否决等固定原因，无法证明时 unknown；不得从自然语言摘要或 rounds==max 反推。保留 Condition/Case/Repetition/Turn 归属，兼容旧记录；不保存模型总结、提问正文、原始命令或凭据。

## F4 与工具使用

位置：`src/tricoder/tools/write.py:CreateFileTool.run`，284–285 行。当前已存在目标在审批前被分类为 policy_denied，默认 stop_task；与人工是否批准无关。

要求仅将“正常工作区内、执行前已存在的普通文件”分类为无效果的可恢复前置错误，明确引导 read_file/edit_file；自动覆盖不允许。目录、敏感路径、链接、只读、审批拒绝、审批后目标变化、父目录身份变化、清理未确认继续严格处理。补固定 detail_code 以便后续区分真实拒绝原因。

本轮历史还有 9 个 patch_count_mismatch：需要帮助模型构造正确补丁及优先用简单编辑，但没有证据证明 parser 错误接受/拒绝。保留 parser 语法和原子预检，仅增加确定可计算的数字诊断/示例。不要将这类模型错误伪装成权限问题。

## 评测契约与预算的最小调整

现有 AND 和隐藏 verifier 保留。新版本题面明确非交互条件，以及 3 道 memory 首轮“只确认上下文，不提前修改；后续再交付”的期望；实际编码 turn 不获得隐藏输入或未来修正。固定版本，不能覆盖 quality-v2.1/历史冻结副本。

8/16 轮属于独立实验维度；维持产品默认值和原 8 轮基线。先只生成实验定义并 dry-run；真实调用不属于本次核查/实施包的必做验收，待用户要求重跑时再执行。真实比较应同时报告产物、完整、turn 原因、用量、耗时，不能把合成 9 轮成功写成模型收益。

## 交付结构

- Agent/工具修复计划：`docs/superpowers/plans/2026-10-20-agent-delivery-feedback.md`。
- 评测诊断、契约与预算实验计划：`docs/superpowers/plans/2026-10-20-eval-delivery-contract.md`。
- coding 提示词：`docs/superpowers/plans/2026-10-20-delivery-fixes-coding-prompt.md`。

实现顺序：先 F1/F2 和工具边界、再诊断/新题面，最后准备预算实验。两包独立验收，第二包不以掩盖第一包失败为目标。原搜索 P2、R1/R2 关闭状态不变；Replan 继续搁置。
