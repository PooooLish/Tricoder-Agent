# Quality V3 迁移

实现完成，待独立复审。原 quality-v2.2 全部普通文件来源哈希见
`source-v2.2-sha256.json`；旧 suite、11 道 contract、冻结副本及成绩保持原样。

- 原 19 道 quality 的业务接口保持；公开输入域、边界和三组隐藏行为测试补齐，增加 README 和可发现公开测试。原 19 道一律 dev，已接触的旧 holdout 不再作为未接触样本。
- 新增 CSV、带时区排序、配置覆盖（dev）以及 CLI、记录兼容、分页合并、包 API 迁移、JSONL、部分撤回、缓存隔离、订单流水线（holdout），新增 11 道全为 quality。完整清单见 case-matrix。
- `cf-module-interface` 同时允许 formatter.py 和 api.py，required 为单个 `*.py`，接受修定义或修调用方；所有题使用根路径同层匹配，tests/*.py 不匹配嵌套测试。
- `rc-provider-transient` 的旧机制只是重试计数，本版保留 ID 而改为纯 increment 题、single_file、faults=["none"]，删除恢复维度；旧机制未改。本题不贡献恢复能力结论。
- `rc-search-truncated` 明确工具层 read/search/glob 模拟 OUTPUT_LIMIT 与业务前缀查找分离；其他模拟错误也不宣称来自真实参数或传输故障。
- memory 首轮文件只包含当时已公开或两轮共同成立的约束；后轮指令不提前注入。新 partial-revision 首轮实际实现前缀及 strip，后轮撤回前缀且增加 lower；真实 Runtime 路径验证两轮及 AND，不只看最终 verifier。
- 每个 user_turn 16 轮、16000 上下文字符；只对新 benchmark 生效。四组实验 30/90/22/8 trials，DeepSeek flash、seed23、memory off/off；本轮只 dry-run，未运行真实模型。

每题隐藏验收分正常、边界、回归三组，输入域以 case 的公开契约为准。
矩阵的 business_contract 映射隐藏三组、public_baseline 映射公开基础例子；
公开基础例子不宣称覆盖整个业务契约。memory 最终需求只能在第二轮出现，
因此不将最终断言加入首轮公开测试。

已有覆盖与新漏检分别记录：旧隐藏验证的错误变体自检结果、所有中途失败、
120 项基础与 4 项合法替代实现的逐项进程退出码/测试数/modified_files、
真实 Runtime 与评分负例均存放在
`runtime/quality-v3-expansion-20261020/implementation-b29568ac93/`。
作者已接触所有新题及 holdout，只能声明尚无真实模型试跑，不能声明盲测。

来源题库、版本与题数均改变，分数不可用于严格归因 Agent 改进。
文件级评分不构成目录级审计；标准库/Windows 离线结果不替代跨平台和真实模型质量验证。

## 2026-10-20 Q3 复审修复增量

本包沿用未验收的 quality-v3 版本，不回写旧分数或重新解释旧日志。
仅更新时区排序、CLI、缓存与包迁移的隐藏验证器：完整记录内容验收、
参数数量的双入口验收，以及保留真实目标调用而不限制次数/等价参数表达。
正常/边界/回归方法名及现有矩阵映射不变；不修改公开业务要求和 workspace。

原变体实际为 120 基础 + 5 合法替代（含 slots）=125 项，全部保留。
新增 26 项评分回归另存于 `tests/test_eval_quality_v3_review_fixes.py`，包含
字段丢失、双入口不同步、深/浅副本、多次/关键字/slots/预处理与接口绕过。
空串合法快捷分支、整数枚举返回码也可接受；接口观察仅证明观察到的使用及业务行为。
本包证据独占目录：
`runtime/quality-v3-review-fixes-20261020/implementation-ff3c0f67b3/`。
旧 reviewer 八例的新期望是两个错误 failed、六个合法 passed；原证据仍保存旧现象。
实现完成，待独立复审；原 Q3-R1/R2/R3 暂不关闭，不提前运行真实模型。
