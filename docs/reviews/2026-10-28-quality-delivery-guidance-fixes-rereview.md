# 交付效率与验证指引：独立复审（2026-10-28）

**结论：通过本轮约定范围复审，未发现新增阻塞问题。** 这是代码、测试与离线题库合同的验收，不是新的真实模型质量成绩。此前V3真实90次仍为artifact90/90、delivery87/90；本轮不追认原三个失败，不宣称pass@k、token或耗时已改善。

## 审查基线与范围

- 实施包：`runtime/quality-delivery-guidance-fixes-20261027/implementation-c1c5dafe99/`。独立核对928份actual before、1138份after与当前工作树；210新增、16修改、0删除，未以HEAD当作before。
- before中的821份源码/测试/脚本/题库与上一轮reviewer留存hash一致，确认不是仅依赖实施者自报基线。5个生产文件为budget_feedback新增及loop、verification_feedback、protocols、tools/write修改；量化指标、完成判定、命令/覆盖、进展守卫、权限、资源所有权与终止逻辑未修改。
- 对照方案：`docs/superpowers/plans/2026-10-27-quality-delivery-guidance-fixes.md`。本reviewer未修改src/tests/evals；只新增独立证据与本报告，并追加交接。

## 主要审查结论

| 项目 | 当前实现与独立验证 |
| --- | --- |
| 写入错误建议 | `tools/write.py`保留结构/计数detail和数字diagnostics，增加精确edit替代；已有文件仅内容未知/变化时建议回读。解析、审批、原子写入和错误恢复级别不变。真实工具恢复链与既有拒绝/未知副作用回归通过 |
| 预算提示 | `engine/budget_feedback.py`与`loop.py`按首轮及剩余≤3轮追加，16轮为1/14/15/16；JSON type=execution_budget，kind=protocol_feedback。未重写system/历史，不自动完成或增加请求额度 |
| 消息、用量与记忆 | 独立native/legacy × off/structured四组16轮请求检查中，首轮估算、其余15轮全部使用provider锚；每个时点预算消息准确且原始请求可见。各组usage输入1600、输出160，不遗漏提示所在请求。原始配对/序号、跨turn和摘要边界回归通过 |
| 覆盖建议 | `verification_feedback.py:_add_next_step_hint`只补展示；当前可信成功但覆盖不足时提示下一步，旧失败同检查复验优先；零测试、stale/forged、unknown、cleanup、legacy_unknown边界保持。读取不解除pending，其他成功检查不抹除旧失败 |
| 完成/权限 | 9组before/current真实Runtime差分，完成状态、stop_reason、轮数和Provider请求数相同；定向测试→读取→根检查、最后一轮仅检查、explicit incomplete、审批拒绝、native同批检查后finish均符合原合同。新增的是预算通知及覆盖下一步提示 |
| V3.1 | 原V3 204文件保持；新包205文件，37个来源文件的差异恰为30题README、suite、4experiment、README/migration，另增来源manifest。业务fixture、case.toml、public/hidden测试、case-matrix字节不变；不混合新旧benchmark |
| 测试调整 | 逐项检查8个旧测试文件的消息位置/序号适配及test-only helper变化；原安全、隐私、失败、配对断言保留。budget过滤只用于旧断言视图，先校验新消息，生产请求不被过滤；独立raw请求探针另行验证 |

没有需要coding session继续修复的已确认阻塞发现。预算反馈/建议能否提高模型自主执行效率，仍需真实复测。

## 本轮实际执行结果

- **独立全量：2259项，2244通过、15skip、0失败、0错误，exit0。** unittest 828.778秒，进程墙钟838.063秒。完整日志`runtime/dgr28/full.log`；全部Eval 577项包含在此次全量中，没有将重复运行次数相加。
- 新V3.1专项183项包含在全量：30真实SessionRuntime链路、34个user_turn、31次真实公共检查，发现数量均>0；三个确认turn不要求无关测试。151变体收据按预期50接受/101拒绝，无超时、cleanup_failed或输出超限。fake Provider不代表自主恢复或模型质量。
- reviewer额外独立探针：预算1至128轮共8,256个时点；四组协议/记忆矩阵逐轮锚核对；before/current各9组生命周期场景，共18次Runtime回放。见`anchors.json`、`differential.json`和对应脚本/原始输出。
- 独立八次CLI dry-run：旧/新各30/90/22/8，exit0；未构建真实Provider或读取密钥。suite逐文件差异和计划身份核对见`suite.json`。
- 原搜索P2的同任务延迟至少5.2秒后搜索/compile/finish，以及清理未确认保留owner测试已包含并通过；取消、已有终止期限、普通命令及其他资源回归随本次全量通过。原P2维持关闭，本轮没有更改相关机制。
- 当前1030份src/tests/scripts/evals文件前后hash不变；旧eval561份（含V3 204份）、指定历史1061份及上一轮历史19,961份hash保持。运行锁和用户数据库未读取/未hash。见`preservation.json`与最终索引。

## 限制与后续

- 本次真实模型调用数为0。新提示的行为收益、缓存/耗时变化及V3.1真实成绩未验证；不能用假模型通过代替它们。未来调用需用户另行授权，建议分别保留Agent-only/V3与指引变更/V3.1结果，不选择性补跑三个失败后报告全量提升。
- 当前Windows环境，15项skip详见`test-summary.json`，涉及平台/链接/锁等限制；其他平台未验证。
- 实施方保留的首次长cwd及旧位置断言失败，本轮全量未复现。已确认最终旧断言适配保持主要语义；不把旧日志改写为通过，也不以本轮结果宣称所有环境基础设施问题消失。
- 原真实失败的完整argv/旧失败stdout仍不可恢复；本轮受控回放验证当前合同，不冒充原90次轨迹的精确因果重放。
- 不读取真实凭据、.env.local或用户数据库，不安装依赖、不提交/推送，不提高默认权限或预算，不自动推进Replan。Q3-R1/R2/R3与量化指标既有结论保持。

## 证据与交接

新证据根：`runtime/dgr28/`。`prepare.json`/`actual-delta.json`/`code-tests.diff`是独立增量核对；`full-started.json`/`full-result.json`/`full.log`是本轮实际命令；`receipts/`为新收据；`test-summary.json`、`anchors.json`、`differential.json`、`suite.json`、`preservation.json`为独立汇总；最终source/旧证据核对与文档hash见final-verification/evidence-sha256。

下一步仅等待用户决定是否授权真实复测；本次复审不自动调用模型。
