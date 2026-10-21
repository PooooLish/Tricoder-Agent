# 2026-10-20 交付修复独立复审

结论：**本次约定范围通过，无新增阻塞发现。F1/P2、F2/P3、F3/P2 关闭；原搜索超时 P2 维持关闭。** 这只代表本轮实现与离线验收通过，不代表真实模型完整交付率已提高。

审查依据为 `2026-10-20-delivery-failure-investigation.md` 及 Agent/Eval 两份交付修复计划。实际前态使用 `runtime/delivery-fixes-20261020/implementation-334076fa24/before/`，未把 HEAD 当作精确基线。独立证据保存于 `runtime/delivery-fixes-20261020/review-20261020/`；作者测试仅作参考，下述测试和核对均由 reviewer 本轮执行。

## 发现关闭与代码核对

| 项目 | 当前代码位置 | 独立结论 |
| --- | --- | --- |
| F1 验证反馈虚报通过 | `src/tricoder/engine/verification_feedback.py:162`、`:86` | 读取现有 scope、证据 authority、失败/过期检查及覆盖并集；旧失败未解除时保持 pending/stale_checks，即使 pending_paths 为空。重建序列的 completed 仍被门禁拒绝；重跑同一失败命令后反馈 satisfied、完成成功。未修改 tracker 的清除规则或调用第二次完成判定。关闭。 |
| F2 失败任务误报未归属 | `src/tricoder/session/runtime.py:1653` | attributable=true 的轮数耗尽任务现在提示尚未完整交付；外部变化、未知影响、清理问题继续独立处理。失败仍保留待确认状态，不推进成功基线。关闭。 |
| F3 finish/停止诊断缺口 | `src/tricoder/engine/loop.py:560`、`:643`，`src/tricoder/evals/service.py:118`、`models.py:159`、`report.py:291` | finish 声明与最终接受分列；同轮数的预算耗尽和 needs_input 可区分。显式 incomplete、验证阻塞、取消、清理失败、未知影响、provider 异常及后置否决有独立回归。逐 turn 重置，不把 finish 工具成功算作完成；旧缺失字段仍 unknown。关闭。 |
| 已有普通文件恢复 | `src/tricoder/tools/write.py:283` | 仅策略检查后的审批前普通文件返回 invalid_argument/replan/file_already_exists，无审批、无文件效果。native/legacy 同任务可继续编辑、验证、finish。目录、只读、敏感/越界和审批后竞态仍停止；链接分支代码检查，实机链接测试跳过。通过。 |
| 补丁反馈与工具说明 | `src/tricoder/patches.py:36`、`:219`，`src/tricoder/tools/write.py:521`，`src/tricoder/protocols.py` | 计数诊断仅取确定的 EOF 计数、白名单整数和固定上限，其他不确定情形省略；未放宽补丁语法。工具选择、零测试和覆盖说明符合计划。通过。 |
| 评测契约与预算准备 | `evals/quality-v2.2/`，`scripts/prepare_delivery_budget_experiment.py:38` | 新版本保留 30 IDs、19 quality/11 contract、8 轮及范围；只有质量题公开交互说明发生约定变化，前置确认未泄漏未来决定。11 contract 及全部 workspace/verifier 原样保留。预算两组仅 19 个 quality max_rounds 为 8/16，严格比较器继续拒绝跨预算比较。通过。 |

检查实际增量未见默认权限/轮数、普通命令策略、Git helper、评分 AND、隐藏 verifier、资源释放顺序或清理门禁的放宽。旧 `test_agent.py` 的已有文件用例改为恢复后完成，仍断言原文件字节、此前验证状态及错误分类，符合已批准的恢复契约。

## 本轮实际验证

- **228 项聚焦及相邻回归，225 通过、3 跳过、0 failures/errors，87.323 秒，exit 0。** 完整命令和耗时见 `focused-command.json`，输出见 `focused.log`。覆盖新交付诊断、验证反馈、Runtime、create_file、补丁、Eval、取消、子进程清理、workspace gate、审计和旧 R1/R2。
- **13 个独立场景断言通过。** 将上一轮 reviewer 探针复制到新目录，仅调整输出位置、新诊断读取及预期修复断言，再实际执行 Runtime/Agent/工具；未使用作者重放结果替代。包括纯确认、前置失败后成功仍整体失败、三种旧失败重建、定向测试加语法检查、同命令复验、同一 9 轮序列的 8/16 预算，以及归属诊断和已有文件零副作用。证据为 `independent-probes/`、`probe-assertions.json` 和三份原始日志。
- 原搜索 P2 的真实同任务回归重新执行：正则超时后等待至少 5.2 秒，再搜索、compileall、finish 成功；清理未确认则停止并保留原 owner。取消、已有终止期限、普通命令清理的相邻测试通过。原 P2 维持关闭。
- 独立生成预算副本；禁止生成器加载配置的探针通过，共享代码快照逐项匹配当前源码。新主 manifest、budget8、budget16 三组 CLI dry-run 均 exit 0、各计划 19 trials。仅准备，没有真实模型调用。
- 旧 v1/A/B/v2.1 原生 JSON 经当前解析器读取并在新目录渲染，历史 passed 仍为 **6/1/1/2**，旧诊断缺失保持 unknown，原文件未回写。
- 校验 **572 份 before、683 份 after** 快照及其索引；文档更新前当前文件全部匹配作者 after。**2057 份保护证据**未变；本轮审查前后的 **592 份源码/测试/脚本/题库文件** hash 未变。旧题库来源与新旧夹具精确差异测试通过。见 `integrity-and-compatibility.json`。
- `git diff --check` 通过；新增/修改相关文本文件另做尾随空白检查。未修改生产代码、测试、题库或脚本。

核对脚本首次把旧结果文件名写成 `report.json`，因文件不存在退出 1；改用实际 `result.json` 后重新执行全部核对，exit 0。初次日志 `verify-integrity.log` 与修正后 `verify-integrity-corrected.log` 都保留；此为 reviewer 脚本路径错误，非产品失败。

注意：旧探针 `budget_8` 实际为第 8 轮 finish 被验证阻塞，不能按名字推断为预算耗尽；本轮读取真实逐 turn stop_reason，另用 `same_sequence_budget8` 验证预算分支。聚合 RunResult 沿用旧合并逻辑，不作为逐 turn 诊断来源。

## 未验证范围与交接

3 个跳过均因本机账户不能创建符号链接，分别涉及 create_file 内部链接、Eval fixture 链接和预算源链接；不声称已实机验证这些边界或 POSIX 行为。本轮没有重跑作者的完整 1815 项、其他操作系统/Python、真实网络故障或真实模型。

最新真实成绩仍是 quality-v2.1 的**产物 15/19、完整 2/19**。quality-v2.2 与预算 8/16 尚无真实结果；离线 9 轮序列不能证明模型收益。历史 mm-latest-correction 原始命令/outcome 缺失的限制，以及文件级评分快照的限制继续保留。

下一步仅在用户要求时执行冻结实验或指定其他工作；不自动重跑真实模型、不调整默认权限、不继续 Replan 等历史待办。原 R1/R2 与搜索 P2 保持关闭。审查过程未读凭据或用户数据库、未安装依赖、未提交/推送、未派生 Agent。
