# Quality V3 独立复审（2026-10-20，暂不通过）

用户要求“复审，通过后真实模型复测”。本次独立审查发现3项P2评分问题，因此尚不满足真实复测的前提；没有启动真实模型或读取凭据。问题均位于新题库的验收脚本，不是本次新增Agent生产缺陷。

## 发现

### Q3-R1 / P2：时间排序只比较id，丢弃记录内容也通过

位置：`evals/quality-v3/cases/sf-aware-time-order/verifier/test_hidden.py:8`、`:14`、`:20`。

公开契约是排序事件记录且保留内容。三个测试只从返回值提取id；回归中的deepcopy只检查输入没有被改，未检查输出完整性。独立反例按正确时区顺序排序后返回`[{'id': e['id']} ...]`，丢弃timestamp与payload/其他字段，公开与隐藏测试全部通过，真实隔离评分为passed。独立业务oracle比较完整返回记录，明确失败。

修复应比较排序后的完整记录内容，包含嵌套字段，保留稳定顺序与不改输入检查；允许返回原记录或等值副本，不新增身份相等要求。将字段丢失实现纳入错误变体。证据标签：`time-control`、`time-drops-record-fields`。

### Q3-R2 / P2：CLI未测试多余参数，违反“恰好两个”仍通过

位置：`evals/quality-v3/cases/cf-cli-result-contract/verifier/test_hidden.py:12-14`。

题面明确恰好两个整数参数，当前只测缺参数和非法整数，未测第三个参数。独立反例只在`len(argv)<2`时拒绝，随后解析前两个参数，静默忽略其余参数；公开/隐藏测试与隔离评分均通过。实际执行`python cli.py 1 2 3`返回0、stdout为3，而公开契约要求参数错误、退出2。

修复应同时验证直接main和实际CLI的多余参数情况、stdout/stderr与退出码；加入第三参数被忽略的错误变体，不仅增加正常加法例子。证据标签：`cli-control`、`cli-ignores-extra-arguments`。

### Q3-R3 / P2：用“恰好调用一次”误拒业务等价实现

位置：`evals/quality-v3/cases/cf-cache-copy-isolation/verifier/test_hidden.py:27`；`evals/quality-v3/cases/cf-package-api-migration/verifier/test_hidden.py:22`。

公开契约分别要求service调用get、report使用包的新导出，没有限制调用次数；两处assert_called_once_with却添加了不可见约束。独立实现使用目标函数判断结果后再调用以返回结果，业务、异常、隔离及公开测试均正确，却被隐藏测试因调用两次拒绝。

例如缓存service为`return cache.get(key) if cache.get(key) is not None else {}`，对应缓存仍用真实deepcopy保护put/get。数据域为dict/list，不会因该条件改变结果；已有正常/缺key/嵌套保护检查通过，唯一失败为once计数。包迁移的两次normalize_label版本同样只因once失败。对照的一次调用版本均通过。

修复应继续验证实际调用目标与正确业务输出，兼容合法多次调用和现有slots/关键字调用；不通过删除调用观察放过绕过get/新包导出的错误，也不反向修改题面强迫使用参考实现。证据标签：`cache-control`、`cache-valid-two-get-calls`、`package-control`、`package-valid-two-export-calls`。

## 独立验证结果

- 全部Eval测试：**309项，307通过、2跳过、0失败/错误，321.688秒，exit0**。含新137项题库测试；没有把实施方日志当作验收证据。
- 重新执行125项已有隔离变体：90错误按预期拒绝，35正确/替代解通过；逐项收据位于`eval-receipts/`。这些现有测试通过仍未覆盖上述反例。
- reviewer另写8次真实隔离评分：4个正确对照通过、2个错误实现误通过、2个合法实现误拒。所有反例公开测试均通过；独立oracle对应返回结果/CLI行为已记录。探针exit0表示成功复现预期缺陷，不表示题库通过。
- 探针通过生产load_suite/run_isolated_case、真实workspace与隐藏验证子进程；只合成Agent完成状态以单独检查评分器，未伪称真实模型或新Runtime质量成绩。真实Runtime路径由此次Eval回归覆盖。
- 四个CLI dry-run均exit0，计划30/90/22/8，均为quality；没有真实Provider调用。
- 实际before696/after905文件及当前文件逐个核对，209新增+3文档修改，共212项实际增量；未使用HEAD作before。813份源码/测试/脚本/题库与7880份受保护历史文件在审查前后hash未变。

## 已确认与边界

30quality/0contract、22dev/8holdout、16轮/16000上下文、Provider计数题迁出恢复分类、截断描述、旧版本保全、范围/AND及多轮消息隔离相关既有测试通过。生产src未改，旧搜索P2、验证状态V1/V2及既有关闭发现维持原结论；本次新增发现编号为Q3-R1/R2/R3。

本轮未重跑完整1975项全项目回归；实施方的全量结果仅作背景。两个Eval skip为当前Windows账户的符号链接权限限制，不计通过。未验证其他平台、真实Provider、长程记忆、实际网络故障或目录级审计。有限错误变体不证明评分器无其他漏检。

## 证据与下一步

新证据：`runtime/quality-v3-expansion-20261020/review-20261020/`。主要文件：initial-verification.json、eval-tests.log、eval-tests-completion.json、eval-receipts/、probe_scoring.py、probe-scoring.log、probe-results.json、probes/*/reviewer-result.json、dry-*.log、final-verification.json。全部独立保存，未覆盖旧日志。

由coding修复上述隐藏测试并增加相应正反例，保留当前实际before与所有日志；不能改Agent、默认权限、AND或旧成绩来解决评分问题。修复后再次独立复审；通过后可按用户本次条件授权执行30题DeepSeek deepseek-v4-flash、16轮、memory off/off、每题一次的新基线。本轮未执行真实复测，原因是复审未通过，而非密钥/费用授权不足。
