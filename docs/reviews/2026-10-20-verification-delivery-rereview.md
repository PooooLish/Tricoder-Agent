# 验证状态与交付纠正：独立复审

2026-10-20。结论：**本轮约定范围通过，未发现新增阻塞问题。V1/P2、V2/P2关闭，V3有界纠正通过。** 原搜索超时P2、F1/F2/F3、R1/R2保持关闭。本结论只覆盖本包代码契约，不表示真实模型完整率已经提高。

## 审查依据与精确增量

读取workspace/project AGENTS、README、pyproject、最新交接、修复计划及实施报告。基线使用 `runtime/verification-delivery-fix-20261020/implementation-4a468f3275/before/`，不是HEAD。

独立重算678份before、682份after副本hash，并逐项核对当前文件与after相同；实际25份增量为8份生产、12份测试、5份文档。重新生成 `independent-code-delta.patch`，核查新增测试、旧负例调整及源码调用链。4453份受保护历史文件全部未变，595份源码/测试/脚本/题库与实施最终快照相同。reviewer随后只新增本报告及更新交接/project状态，未修改生产代码或测试。

## 逐项结论

| 项目 | 代码位置 | 独立结论 |
| --- | --- | --- |
| V1：历史过期造成永久阻塞 | `engine/validation.py:99`、`core/validation.py:100`、`engine/completion.py:179` | 当前有效的认可检查可使报告observed，旧记录仍在stale集合且不提供覆盖；未解除失败优先failed。当前证据仍须authority、完整快照、覆盖与无失败同时满足。原三类阻塞均在before/current对照中修复。 |
| V2：失败检查定位不足 | `engine/verification_feedback.py:137` | 反馈包含宿主检查ID、类型、固定原因、受控目标、省略数量；覆盖齐全仍有失败时明确要求复验。验证了native/legacy反馈、零/过滤检查、敏感路径、省略和2000字符边界，不回显argv或输出。只读审查不额外产生修复义务。 |
| V3：可信失败后的过早finish | `engine/completion.py:110,160`、`engine/loop.py:571` | 仅完整当前快照且检查归属可信时允许一次纠正；缺覆盖与未解除失败共享计数及原预算。before第3轮结束，current第7轮修复、同命令复验并完成；未清空失败冒充通过。 |
| Runtime一致性 | `session/runtime.py:409,2036` | 逐条排除失效ID、跨快照、非认可/无效检查和错误authority；未解除失败不清除跨任务义务。成功场景result与memory的pending均为空。旧stale报告缺失ID时保守拒绝。 |
| 终止与资源 | `engine/loop.py:598-616,781` | 取消、cleanup_failed、UNKNOWN、已启动期限、审计错误等仍停止，且审计返回后再次检查。新旧恢复等待超过5.2秒后可正常执行；已有期限不改写，任务/锁/owner/历史与记忆边界回归通过。 |

没有把不同命令的真实失败合并，也没有改变默认权限、默认预算、命令策略、隐藏verifier、quality-v2.2或AND评分。旧失败测试的响应队列只补充第二次finish以适应一次纠正，仍断言失败、未解除检查及轮数；没有用删除负例换取通过。

## Reviewer实际执行

新证据独立保存于 `runtime/verification-delivery-fix-20261020/review-20261020/`。

### 本轮测试

两组均设置 `PYTHONPATH=src;tests`、`PYTHONDONTWRITEBYTECODE=1`，使用现有 `.venv/Scripts/python.exe -X utf8 -B -m unittest ... -v`：

1. `test_verification_delivery_state test_blocking_check_feedback test_unresolved_finish_recovery test_task_validation test_finish_coverage test_finish_verification_recovery test_verification_feedback_consistency test_verification_obligation test_runtime_coverage test_delivery_stop_diagnostics test_search_timeout_recovery test_verification_evidence`：214 tests，213通过、1 skip，204.745秒，exit0，日志 `focused.log`。
2. `test_protocols test_audit test_delivery_budget_experiment test_eval_diagnostics test_agent`：114 tests，113通过、1 skip，42.255秒，exit0，日志 `compatibility.log`。

合计 **328 tests，326通过、2跳过、0 failures/errors**。两个跳过分别是 `SnapshotTests.test_symlink_is_not_followed` 与 `BudgetExperimentTests.test_link_source_refused`，均因本Windows账户不能创建符号链接。跳过不计通过。实施方的1838项全量记录仅作参考，未计为本次独立运行。

### 实际before/current对照

新写的 `replay.py` 分别导入实际before/src与当前src，验证 `tricoder.__file__` 的真实来源。每版13种场景×native/legacy两协议，各26次，共52次隔离Runtime运行；使用队列Provider、真实文件工具/命令进程与新建临时SQLite，不接触真实模型或用户数据库。

| 场景 | before | current |
| --- | --- | --- |
| 旧成功检查→修改→不同命令当前全覆盖 | 拒绝 | 成功 |
| 旧成功普通脚本→修改→当前全覆盖 | 拒绝 | 成功 |
| 旧零测试→创建测试→当前tests+syntax联合覆盖 | 拒绝 | 成功 |
| 修改后仅普通脚本 | 拒绝 | 拒绝 |
| 仅旧stale、无当前检查 | 拒绝 | 拒绝 |
| 真实失败→修改→仅syntax | 拒绝 | 拒绝，保留1条失败 |
| 真实失败→修改→不同签名测试成功→syntax | 拒绝 | 拒绝，保留1条失败 |
| 真实失败→修改→原签名复验成功→syntax | 成功 | 成功 |
| 真实失败→过早finish→修改→原检查→补覆盖→finish | 第3轮拒绝 | 纠正一次，第7轮成功 |
| 纯确认 | 成功 | 成功 |
| 明确incomplete | 失败终止 | 失败终止 |
| 当前检查后外部变更 | 拒绝 | 拒绝 |
| 当前测试仅覆盖tests，源码未覆盖 | 拒绝 | 拒绝 |

全部符合预期。current成功场景的会话待验证路径同时清空，失败场景保留失败/义务。记录使用收尾前 `finish_assessment`，不把Runtime失败后的失效状态当原始原因。结果见 `replay-before.json`、`replay-current.json`，退出码均0。

首次before启动发生reviewer脚本命名碰撞：`inspect.py`遮蔽标准库inspect，在导入dataclass时失败；未进入Runtime。改名为 `verify_snapshot.py` 后重新运行，失败日志 `replay-before.log` 和成功日志 `replay-before-attempt2.log` 均保留，不将脚本错误算作产品缺陷。

## 限制与下一步

- 本轮没有重跑完整1838项、真实模型、其他OS、真实MCP或网络故障。最新真实结果仍为16轮组产物19/19、完整4/19；修复后的质量收益未知。
- 保留设计限制：仅剩stale且无当前可信检查锚点仍停止；失败仍须原签名有效复验；超过历史窗口的检查仅保留ID，复杂/非ASCII目标可能被保守省略；没有命令自动重放或新检索工具。
- 有限工作区快照、既有目录/TOCTOU覆盖边界未扩展，不能据此宣称对任意外部并发变更提供完整隔离。
- 当前复审通过，可进入同题面、同预算的真实模型效果验证；本轮没有自动执行。题面修订与Replan继续独立搁置。
- 未读取凭据/.env.local/用户数据库、未安装依赖、未提交或推送；生产文件保留原状。最终核验与文件索引见本次证据目录的 `final-verification.json`。
