# 交付效率与验证指引修复实施记录（2026-10-28）

状态：实现完成，待独立复审。本记录是实施者自测与自审，不关闭独立审查发现。无真实模型复测，不能声称完整交付率提升。

本轮依据 `docs/superpowers/plans/2026-10-27-quality-delivery-guidance-fixes.md`，按 A→B→C→D 单会话实施。旧真实基线仍为 DeepSeek deepseek-v4-flash / V3 / 30×3：artifact90/90、delivery87/90；三个失败仍计16轮耗尽，不追认独立oracle为交付。

## 完成矩阵与实际增量

| 任务 | 实现与位置 | 本轮验证 |
|---|---|---|
| A 工具替代建议 | `src/tricoder/tools/write.py` CreateFileTool.description、已有普通文件分支、_patch_feedback；`src/tricoder/protocols.py` COMMON_SYSTEM_PROMPT | 四类结构/计数错误保留原detail/count，提示已读单文件用非空精确唯一old_text的edit；未知/变化才read，必要多文件仍patch。native/legacy真实Runtime错误→edit→check→finish，错误无审批/副作用，后续仍审批；原policy/approval/UNKNOWN停止回归 |
| B 预算 | 新`src/tricoder/engine/budget_feedback.py` build_round_budget_feedback；loop.run取消检查后、normalize/prepare前 | 纯函数验证整数/非bool/范围；首轮与剩余≤3，16轮为1/14/15/16，1/2/3不重复；JSON≤800，kind=protocol_feedback；实际prepare→Provider、off/structured×native/legacy、usage前缀1→2/13→14→15、消息配对/序号、取消/跨turn摘要/协议计数/最终未finish |
| B 覆盖 | `src/tricoder/engine/verification_feedback.py` _add_next_step_hint | 当前可信成功但pending才提示verify_pending_paths，读文件不覆盖；旧失败repair_blocking_checks优先，同检查复验不变；零测试、stale/forged、脚本、unknown/cleanup/legacy、纯确认沿用门禁。固定条件示例不拼路径，原2000字符/20路径裁剪不变 |
| C V3.1 | 新`evals/quality-v3.1/`205文件；根README当前套件说明 | suite quality-v3-1 / benchmark quality-v3.1，30quality/0contract，22/8，16/16000，四清单30/90/22/8。37来源文件有意变化（30README、suite、4experiment、2方法文档）；新增source-v3-sha256；case-matrix、公私测试、业务源码、全部case.toml字节一致 |
| D 交接 | 本文、project.md、reviewer-handoff追加；runtime独占证据 | 专项、Eval、全量、八dry-run、actual before/after/delta、历史hash及自审，见下 |

新增测试：`tests/test_delivery_guidance.py`、`tests/test_round_budget_feedback.py`、`tests/test_eval_quality_v3_1_guidance.py`。最后一项复用旧125+26变体方法，但将case绑定新suite、收据分目录，不删旧断言/变体。`tests/quality_v3_support.py`仅新增可选workspace_prefix，默认保持旧行为，用于Windows长cwd限制。

旧测试适配：test_eval_public_contract、test_memory_summarizer、test_session_integration、test_agent、test_agent_refactor_contract、test_agent_termination、test_batch_failure、test_conversation_memory。新增普通历史消息使旧固定位置/序号/“最后user就是task”假设失效；保留原安全/隐私/历史/配对断言，显式确认预算消息或用仅供断言的预算验证后视图。真实Provider请求和持久历史未过滤；独立新增测试直接核对原始完整请求与usage锚。未改Agent完成门禁来迎合旧测试。

## RED / GREEN 与失败保留

- A-red：3 tests，6子例断言失败+2测试工具None内容拼接错误；有有效的缺替代建议RED。A-green仍2拼接错误，修正测试后A-green-2为46 tests、1 skip、exit0。
- B-budget-red首轮有测试遗漏SessionContext参数；B-budget-red-2完成真实链路后8缺预算断言失败+1新模块缺失。B-budget-green 5 tests exit0。补充既有摘要/边界回归不虚构额外RED。
- B-feedback-red 6 tests、5子例失败；B-feedback-green25 tests exit0。最终专项另见计数表。
- C-red：新suite尚不存在（1断言失败、4加载缺文件）；C首轮183 tests有6框架verification_error。诊断脚本确认CreateProcess cwd263字符/WinError267，不作为业务拒错证据；缩短测试临时目录prefix后C-green-2为183/183，0skip，194.881秒。保留每次收据。后补多轮公共输出按tool_call_id去重（非按内容）确保两轮相同输出也各有收据，mm-partial-revision专项1项通过。
- D首轮Eval577 tests有1旧消息定位失败、2skip。定向RED1→模块GREEN5；消息相关21项首次2失败、首次调整仍2子例失败，随后21通过。
- D Agent旧断言RED84 tests为16子例失败+1位置索引错误；中间BOM编辑脚本未执行成功、替换变量名错误均保留，最终关联146项通过。首轮全量和最终全量结果如下，不隐去首次失败。

| 日志/命令组 | tests | failures / errors | skips | exit | unittest秒 |
|---|---:|---:|---:|---:|---:|
| D-patch_feedback | 8 | 0 / 0 | 0 | 0 | 0.568 |
| D-round_budget_feedback | 6 | 0 / 0 | 0 | 0 | 8.355 |
| D-delivery_guidance | 10 | 0 / 0 | 0 | 0 | 8.561 |
| D-verification_feedback_consistency | 6 | 0 / 0 | 0 | 0 | 4.943 |
| D-blocking_check_feedback | 5 | 0 / 0 | 0 | 0 | 1.997 |
| D-context_manager | 8 | 0 / 0 | 0 | 0 | 0.003 |
| C-green-2 | 183 | 0 / 0 | 0 | 0 | 194.881 |
| D-eval | 577 | 1 / 0 | 2 | 1 | 438.956 |
| D-eval-2 | 577 | 0 / 0 | 2 | 0 | 491.131 |
| D-full | 2259 | 20 / 1 | 15 | 1 | 936.171 |
| D-full-2 | 2259 | 0 / 0 | 15 | 0 | 975.572 |

最终全量2259项=2244通过+15skip，0失败/错误；全部Eval577项=575通过+2skip。原27项专项在D日志分别重跑8+6+5+8通过，不使用旧结果作为证据。八个CLI dry-run均exit0，两套各30/90/22/8；日志dry-quality-v3*。平台skip逐项见platform-skips-final.json：Windows链接权限、打开锁文件的rename/replace限制、Linux抽象socket以及控制字符文件名能力。

所有命令使用现有`.venv/Scripts/python.exe -X utf8 -B`，环境PYTHONPATH=src;tests、PYTHONDONTWRITEBYTECODE=1；各轮独占TRICODER_V3_EVIDENCE。完整argv/退出码/墙钟在`test-results-index.json`和每个同名JSON，完整日志同名.log。重跑计数不相加冒充独立测试数。平台skip不计通过。

## 新题库实际验收

30个隔离case经fake Provider和真实SessionRuntime/工具审批/命令/finish链路完成，共34 user_turn，其中3确认轮仅finish，mm-partial-revision分别按首轮和修正轮业务契约编辑与检查；31次根目录公共发现，每次实际发现1项测试，不用手工RunResult成功代替交付。隐藏验证由生产隔离评分入口在Agent结束后注入。

151项变体收据：50个预期passed、101个预期failed，与旧125基础/替代及26复审修复契约一致。所有进程无超时/清理失败/输出限额错误，框架status不是error；6个initial源码本身缺旧API、接口错配、缺新导出、缺字段、未定义调用或除零，unittest以ERROR呈现，不能混同首轮WinError267基础设施错误。26复审变体保持原完整记录、CLI两入口、合法多调用/slots/预处理及绕过拒绝要求。逐项modified_files、公共/隐藏测试数和退出码见收据及C-final-verification-index.json。

本30条Runtime链路专测新默认公开命令和finish；未在该链路额外注入模拟故障。故障机制由既有Eval回归覆盖，不能把固定fake脚本称为模型自主恢复能力。完整AND、无finish失败、旧失败同签名复验、普通命令、取消/cleanup_failed/owner、搜索超时后至少5.2秒延迟恢复均由实际专项/全量回归验证。

## actual before / after / 保护

证据根：`runtime/quality-delivery-guidance-fixes-20261027/implementation-c1c5dafe99/`。HEAD未作为before。

- `before/`、`before-sha256.json`：928份实际安全文件及原脏状态`git-status-before.txt`。
- `after/`、`after-sha256.json`、`delta/`、`delta-index.json`：所有新增未跟踪文件也纳入，逐文件完整内容、SHA256和增量。
- `protected-sha256.json`、`preservation.json`：旧V3、全部旧eval及两个指定历史目录普通证据核对。历史180个.lock属于运行锁状态，未读取/未hash；不读数据库/凭据。
- `test-results-index.json`、逐轮日志/收据、`C-final-verification-index.json`、`self-review.md`、`progress.md`、最终证据hash索引。

最终安全文件索引：before928 → after1138；210新增、16修改、0删除。全部561份旧eval文件（含V3的204份）、指定历史1061份普通文件hash保持；其余既有912份安全文件不变。原量化指标实现及所有非本轮生产源码均保持。

## 限制与下一步

本轮仅Windows/Python3.11与现有依赖离线验证；平台skip、其他OS/Python/真实Provider未验证。预算和反馈能被模型看见，不证明模型采纳；摘要以现有protocol_feedback宿主排除规则约束，fake摘要测试不证明任意真实模型都会遵守。无默认权限/轮数/上下文上限变化，无额外模型调用、自动finish、自动重放或覆盖能力。

V3.1只改公开验证操作指引及版本身份，跨benchmark不能直接归因Agent改进。旧V3及87/90成绩保持；本轮未重新付费跑分，也未修CSV、Replan或历史事项。实现者自审未发现需要扩大生产安全边界的项；交独立reviewer核对后由用户另行授权真实实验。未提交/推送、未安装依赖、未读真实凭据或用户数据库。
