# Quality V3

30 道真实模型质量任务定义，全部 `execution_kind=quality`，没有 contract 题混入分母。本轮只进行离线建题和验收，**未运行真实模型**。实现完成，待独立复审。

原 19 题优化后全部归为 dev；新增前 3 题为 dev、后 8 题为 holdout，共 **22 dev / 8 holdout**。旧 holdout 已用于诊断，不能继续称为未接触题。新 holdout 已由作者和验收脚本接触，仅尚未开展真实模型测试，不承诺作者不可见或训练无污染。

每个 user_turn 固定 **16 轮 / 16000 字符上下文上限**。不改变产品默认轮数、旧题库或旧 8/16 轮实验脚本。memory 的确认轮与编码轮分别消耗各自预算，完整成功按所有轮的 AND；工具成功或最终产物通过不等于 Agent 完整交付。

| 实验清单 | split | repetitions | planned_trials | 时间预算秒 |
| --- | --- | --- | --- | --- |
| experiment-quality-baseline.toml | all | 1 | 30 | 7200 |
| experiment-quality-repeat3.toml | all | 3 | 90 | 21600 |
| experiment-quality-dev.toml | dev | 1 | 22 | 7200 |
| experiment-quality-holdout.toml | holdout | 1 | 8 | 7200 |

四组均为 DeepSeek `deepseek-v4-flash`、seed 23、memory off/off、hidden_verifier；condition faults 为 none，题目自身保留的模拟故障由现有控制器注入。这里只提供定义和 dry-run，不授权模型调用。

类别为 single_file=8、cross_file=11、recovery=4、memory=4、delivery=3。没有 quality safety 题；旧 11 道 contract 保留在旧套件。旧 19 题仍是 micro/regression，新 CSV/时区题 basic，其余新题 advanced；这不等于 30 个大型真实仓库 issue。

每题有 `case.toml`、`workspace/README.md`、`workspace/tests/__init__.py`、可发现的公开测试及独立隐藏 verifier。允许修改范围公开且从根按同层匹配；README、只读生产依赖和隐藏 verifier 不允许修改。公开测试运行：

```text
python -m unittest discover -s tests -v
```

该命令验证公开业务例子；定向 tests 发现不自动证明根源码的宿主覆盖。修改后应按宿主反馈补相关检查，例如根目录 unittest discovery 或适用的语法检查，随后明确 finish。语法检查不替代业务验收。纯确认轮只确认并 finish；memory 的后续修正只在后续 user_turn 出现，首轮 README/公开测试保持共同基线。

`case-matrix.json` 登记来源、类别、split、难度、需求到测试方法的映射、故障证据类型及限制。`source-v2.2-sha256.json` 是旧套件全部 102 个普通文件的来源哈希；`migration.md` 解释迁移。参考正确实现、两种错误变体和额外合法实现仅放在仓库 `tests/quality_v3_support.py`，不会复制给 Agent。

离线验收使用生产 `load_suite`、真实隔离副本、隐藏验证子进程和评分入口。每题 initial/correct/wrong_a/wrong_b 四种，共 120 项；另有 CSV、稳定去重、旧 API 兼容、formatter 调用方修复及使用 __slots__ 的缓存共五项替代实现。CLI 验证真实进程的 stdout/stderr/退出码，包迁移验证实际导入链。初始和错误变体应失败，正确与合法替代实现应同时通过公开/隐藏测试。

`rc-search-truncated` 的模拟 OUTPUT_LIMIT 可发生在 read_file/search_text/glob_files，业务不实现截断。`rc-provider-transient` 只保留 ID 追溯，已改为 single_file、faults=["none"]，不再计恢复能力。其他恢复题也是模拟工具结果，不证明真实 HTTP、真实参数修复或真实大输出恢复。memory 仅覆盖短多轮，不证明长期记忆或跨会话安全。

新旧 benchmark **不可直接** 比较以归因 Agent 改进：题面、边界、题数、划分与预算均有变化。旧版本、旧冻结实验及历史成绩不修改；最近 quality-v2.2 的 16 轮 19/19 产物、16/19 完整成绩不代表 V3 成绩。建题与实现记录不能替代独立复审。

## 2026-10-20 评分器复审修复

Q3-R1/R2/R3 的四份隐藏验证器已修正：时区排序比较调用前独立复制的完整记录
（含嵌套内容、无 id 输入、稳定顺序和输入不变），接受等值副本；CLI 同时验证
`main(argv)` 和真实子进程的三/四参数错误，要求返回或退出 2、stdout 空、
stderr 非空；缓存和包迁移观察真实目标接口调用及业务结果，接受多次、slots、
关键字及等价预处理，不要求恰好一次调用。绕过 get 或包新导出的负例继续拒绝。
调用观察不证明任意程序的完整数据流，也不要求每条合法快捷分支都调用目标。

保留原 125 项变体，另在 `tests/test_eval_quality_v3_review_fixes.py` 追加
26 项真实隔离评分正反例（包含合法整数枚举返回码）。原 reviewer 八例另用修正期望重放，旧探针与旧结果不改；
本轮 before/after 指纹分开保存，旧离线成绩不能当作修复后的证据。
题面、workspace、版本/题数/split/预算、评分框架及 Agent 均不变。
实施记录见 `docs/reviews/2026-10-20-quality-v3-review-fixes-implementation.md`；
状态为“实现完成，待独立复审”，原复审暂不通过结论及发现由 reviewer 继续核查。
