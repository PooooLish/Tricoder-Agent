# 交付阻塞：结合当前代码的独立核查

2026-10-20。用户要求“具体怎么修改？结合具体代码进行检查”。本轮为 reviewer 核查及方案，不修改生产代码。使用当前脏工作树，未将 HEAD 视为修改前基线。

## 结论

应优先修复 Agent 对历史检查、当前有效证据和未解决失败的混用，并补足有界纠正反馈。没有证据支持调整默认权限为 relaxed/fullaccess。真实重跑的 16 轮组产物 19/19、完整 4/19，是上轮结果；本轮没有新增真实模型调用，也不能预报修复后的通过率。

### V1 / P2：已过期的非失败历史成为永久完成障碍

代码链路：

- `engine/tool_batch.py:769`：已确认文件/目录变化调用 `validation.invalidate()`。
- `engine/validation.py:42`：所有非 information 的旧检查均放入 `_stale`，包括成功普通脚本和退出 0 的零测试记录。
- `core/validation.py:84`：检查签名是完整 `(argv, cwd)`。
- `engine/validation.py:85`：新检查只移除同签名的 stale；`:99` 的 report 只要有任何 stale 就汇总为 stale。
- `engine/completion.py:83`：只要报告 stale，就清空当前 evidence，强制待验证；与当前覆盖是否齐全、是否存在真实失败无关。
- `engine/verification_feedback.py:162` 同样把全部 stale 视为阻塞。`session/runtime.py:407` 又以报告 stale 为跨任务义务清除的总开关。

独立真实 Runtime 回放：

```text
python -m compileall -q config.py   # 旧版本成功
edit_file(config.py)               # 已确认修改
python -m compileall -q .           # 当前版本成功且覆盖全部修改
finish(completed)
=> verification_blocked；finish 时 pending_path_count=0、unresolved_check_count=0、status=stale
```

在 finish 前额外重跑第一条原命令，结果变为成功。另两组复现：旧的 `python config.py` 成功记录、旧的 `python -m unittest -q` 零测试记录，都可造成相同阻塞。零测试不应提供验证能力，但也不应凭空新增“必须重跑此命令”的长期义务；之前已有真实失败时则必须保留失败。

这证明当前实现存在不必要的交付障碍，不证明上轮每一道真实题都由此失败。旧真实日志未保留完整命令，不能将合成序列冒充历史逐字回放。

### V2 / P2：反馈不足以定位必须重跑的失败检查

`engine/verification_feedback.py:76-92` 主要提供最新 check_kind、总体状态和 unresolved 数量。`pending_paths=[]` 时，模型仍可能只得到 stale_checks / unresolved_checks，没有具体失败检查 ID、类型、目标及如何解除。真实失败的替代规则严格依赖签名，因此反馈应指出具体阻塞检查，而不能只泛称“继续验证”。

反馈必须来自宿主，保持有界与敏感路径过滤；不能直接把任意 argv、输出、绝对路径或凭据写入反馈/审计。

### V3 / 改进：已知检查失败后的过早 finish 没有纠正机会

`engine/completion.py:106-123` 的 recoverable 条件仅允许 unverified/observed、无 unresolved、无 failed_snapshot 且有 pending_paths。`engine/loop.py:573-636` 仅该分支继续循环，其他检查失败直接终结。

独立回放预置了“创建测试→测试失败→过早 finish→修改代码→同命令测试→compileall→finish”。当前在第 3 轮退出，后续 4 轮动作未请求，尽管预算为 16。该行为符合旧设计和既有测试，不是上轮实现偏离计划；建议本轮明确调整契约：仅对可证明属于当前任务、当前完整快照的普通验证阻塞，允许共用现有的一次纠正机会。继续执行不等于接受完成，旧失败仍须真实复验。

不能把所有 stale 都改成可恢复。finish 前外部变化、authority 异常、扫描不完整、取消、清理失败、UNKNOWN 等仍须停止。详细窄化条件见修复计划。

## 独立验证

新证据：`runtime/verification-delivery-design-20261020/`。

| 场景 | 当前结果 |
| --- | --- |
| 旧成功检查→修改→不同命令当前全覆盖→finish | 阻塞，0 未覆盖、0 未解除 |
| 同上，补跑原命令 | 成功 |
| 旧成功普通脚本→修改→当前全覆盖 | 阻塞，0 未覆盖、0 未解除 |
| 旧零测试→创建测试→当前 tests + syntax 联合覆盖 | 阻塞，0 未覆盖、0 未解除 |
| 同一联合覆盖，不含旧零测试 | 成功 |
| 真实测试失败→修改→仅 syntax | 阻塞，1 未解除；正确保护 |
| 真实测试失败→修改→同测试成功→syntax | 成功；正确解除 |
| 测试失败→过早 finish→预置修复动作 | 第3轮终止，剩4动作；无纠正 |
| 纯确认，不修改、不测试 | 第1轮成功；应保留 |

9 个场景的“当前行为”断言全部满足，不代表问题已修复。`probe.py` 使用队列 Provider，但运行实际 SessionRuntime、文件工具、命令进程和独立临时 SQLite；没有访问模型或用户数据库。结果及收尾前 finish_assessment 在 `results.json`，不能用 Runtime 失败收尾后的 stale 反推根因。

另独立运行：

```powershell
$env:PYTHONPATH='src;tests'
.venv/Scripts/python.exe -X utf8 -B -m unittest test_task_validation test_finish_coverage test_finish_verification_recovery test_verification_feedback_consistency test_search_timeout_recovery -v
```

**73 tests，全部通过，无 skips，26.323 秒，exit 0**。包含原搜索超时后等待超过5秒继续运行与未确认清理保留 owner 的回归。现有测试部分明确断言旧零测试造成 stale 阻塞，因此测试全绿与上述产品缺陷并不矛盾；实现时需有依据地更新这些断言，不能删除真实失败/外部变化的负例。

592份源码、测试、脚本、题库文件探针前后 hash 相同。本轮不重跑完整1815项、真实模型或其他平台；不以本次受控机制验证宣称13个真实验证阻塞已全部解释。

## 交付与后续

- 修复计划：`docs/superpowers/plans/2026-10-20-verification-delivery-state.md`。
- coding 提示词：`docs/superpowers/plans/2026-10-20-verification-delivery-coding-prompt.md`。
- 旧 F1/F2/F3、搜索 P2、R1/R2 保持关闭：本次 V1/V2/V3 是新范围，上一包明确没有改变 tracker 替代与 finish 拒绝规则。
- 题面另有独立歧义：`evals/quality-v2.2/cases/rc-search-truncated/case.toml:5,19` 没明确截断发生在宿主工具，可能被误解为业务函数行为。建议单独版本修订，不与 Agent 代码实验同时改题，不改隐藏 verifier/旧分数。
- 实施交给 coding session；reviewer 本轮仅生成新证据和文档，未更改生产默认值、代码、测试或题库。
