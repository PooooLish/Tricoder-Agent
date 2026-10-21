# TriCoder Quality V3：30道真实质量题扩充与优化 Implementation Plan

> **For agentic workers:** 使用 `superpowers:executing-plans`，按任务逐项实施。用户已选择交给独立 coding session；单会话执行，不派生子 Agent，不提交。步骤用复选框记录实际完成情况。

**Goal：** 将19道真实模型质量题优化并扩充为30道，保留11道独立工程契约题及全部历史版本，建立可解释、可重复验收的新基准。
**Architecture：** 新建纯quality的 `evals/quality-v3/`，沿用现有loader、隔离workspace、隐藏verifier、实验runner及评分。题面、公开测试、隐藏测试、正确解与错误变体相互核对；不修改Agent或评分门禁以适应题目。
**Tech Stack：** 现有Python 3.11+、标准库unittest/csv/json/datetime/argparse、TOML、JSON、Markdown；不新增依赖。
**Spec：** 本文第1—6节是完整需求设计，第7—9节是实施与验收。用户2026-10-20明确选择“真实模型质量题达到30题”，并要求交付文档给coding session。不是新增30题，也不是将11道contract冒充真实质量题。
**状态：** 方案交付，尚未实施；本文件不证明新题库可运行或模型分数提升。

## Global Constraints

- 工作区仅 `D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli`；先读工作区与项目AGENTS.md、README.md、pyproject.toml、project.md、reviewer交接最新记录。
- 保留所有现有未提交/未跟踪内容。HEAD不是before基线，禁止reset/clean、覆盖旧日志或用HEAD重建。
- 不读取`.env.local`、真实凭据、用户数据库；不安装、不调用真实模型、不提交/推送。临时隔离fixture和合成数据库按现有测试使用。
- 本包仅评测数据、评测测试、文档和新版本实验清单。`src/`、Agent提示、工具策略、finish/验证/取消/清理/所有权、默认权限及默认轮数不在修改范围。
- `evals/smoke/`、`quality-v1/`、`quality-v2/`、`quality-v2.2/`、`regression-v1/`及旧测试、旧原生成绩和快照保留字节不变。新增测试文件，不修改旧断言迎合新题库。
- 不修改 `MAX_EVAL_CASES=32`；quality-v3恰30题，contract继续使用旧套件的fake条件独立执行，不能将41题塞入同一suite。
- 保留隐藏验证、正常终止、范围和清理的AND条件；不能通过忽略失败turn、放宽修改范围、删除未验证记录提高分数。
- 使用新benchmark版本。跨v2.2/v3同时发生题面、fixture、verifier、split和预算变化，不解释为Agent单变量对照。

## Review Focus

1. 只修示例、硬编码输出、漏改实际调用方：新旧每题都要有至少两种不同错误变体，隐藏行为验证拒绝。
2. 测试能发现但导入错文件、零测试成功、CLI只检查函数：公开测试实际发现至少一项；包入口和CLI使用真实调用路径验证。
3. 题目省略条件、隐藏测试另加要求：需求—断言映射逐条核对，不把未公开的异常/Unicode/类型行为强加给Agent。
4. 往首轮泄露后轮要求、历史holdout再次用于调参：多轮时序和新split有实际消息测试，记录保留集接触历史。
5. 把模拟工具错误或计数称为真实机制验证：元数据明确注入层与局限，Provider计数题不再包装为真实恢复质量。

## 1. 已核对的事实与参考边界

- 当前quality-v2.2为19 quality+11 contract；原生真实运行只将19 quality计入质量分母。
- 最新同模型真实结果：8轮产物17/19、完整3/19；16轮产物19/19、完整16/19。见 `docs/reviews/2026-10-20-verification-delivery-real-full-rerun.md`。这是v2.2单次结果，不是v3预期成绩。
- `src/tricoder/evals/loader.py`限定32 cases，schema2已有category、split、steps、faults、scope字段，不需要新增schema。
- `src/tricoder/evals/faults.py`：`tool_output_truncated`对read_file/search_text/glob_files任一首次匹配调用生效；不是只对搜索生效。`provider_transient`只增加透明重试计数，不抛真实网络故障。
- 现有19题fixture极小；隐藏验证常只有少量例子。已有测试会验证正确解和初始错误，但缺少系统的多种“看似修好”错误变体。
- 现有旧holdout已反复进入真实运行和失败诊断；不能继续宣称其为未接触保留集。

2026-10-20查阅的官方参考：

- SWE-bench评估指南：https://www.swebench.com/SWE-bench/guides/evaluation/ 。其方式是将生成补丁应用于真实仓库并运行测试；本包参考独立行为验证、固定环境与逐题证据的原则。
- Terminal-Bench：https://www.tbench.ai/ 。作为Agent任务基准的外部参照，本包不导入其任务，也不运行其官方harness。

新11题仍为独立编写的标准库小型工程任务。记录“设计参考”，不要标成SWE-bench/Terminal-Bench题目或官方子集，不复制外部源码/题面/测试，不宣称可与排行榜横比。

## 2. 新版本、组织与实验

### 2.1 目录与版本

创建 `evals/quality-v3/`：

- `suite.toml`：schema_version=2、id=`quality-v3`、benchmark_version=`quality-v3`，固定30个唯一case ID。
- `cases/<id>/case.toml`、`workspace/`、`verifier/test_hidden.py`；全部execution_kind=quality、scorers包含hidden_verifier。
- `README.md`：30题任务目录、类别/难度、来源、命令、测量局限、split及历史对照规则。
- `case-matrix.json`：30条逐题元数据。每条字段为`id/source_case/category/split/difficulty/task_family/requirements/verification_map/fault_evidence/limitations`；source_case为旧相对路径或null。requirements使用本题R1/R2编号，verification_map列每项需求对应公开/隐藏测试名，不保存答案。
- `source-v2.2-sha256.json`：实施前整个旧quality-v2.2的文件相对路径→SHA256，文件筛选显式且覆盖全部正常文件。旧目录不得含链接或不明私密内容，遇到则报告并停止复制。
- `migration.md`：19题逐题记录题面、fixture、verifier、预算、split、故障/类别变化与理由。
- 实验清单：`experiment-quality-baseline.toml`、`experiment-quality-repeat3.toml`、`experiment-quality-dev.toml`、`experiment-quality-holdout.toml`。

不得将测试用正确实现/错误变体、开发探针或旧报告复制进workspace。fixture均为合成内容，不能链接回项目或使用用户数据。

### 2.2 预算与条件

- 所有30题固定每user_turn `max_rounds=16`、`max_context_chars=16000`；这是新benchmark预算，不改产品默认值。任务规模控制为2—5个小生产文件，标准库可完成。
- 条件固定deepseek/deepseek-v4-flash、memory_compaction=off、memory_persistence=off、scorers=["hidden_verifier"]、faults=["none"]；case自身故障仍按定义并集生效。
- baseline：split=all、repetitions=1、max_trials=30、time_budget_seconds=7200、seed=23。
- repeat3：split=all、repetitions=3、max_trials=90、time_budget_seconds=21600、seed=23。
- dev：22题一次、max_trials=22、7200秒；holdout：8题一次、max_trials=8、7200秒；两者seed=23。
- 8轮压力实验只在README给出未来复制新suite到runtime、仅改max_rounds的操作约束，不改旧 `scripts/prepare_delivery_budget_experiment.py` 的v2.2专用契约。本包不新增压力实验自动化、不运行真实模型。

### 2.3 split与类别

- 原19题在v3均标dev；新增前3题标dev，新增后8题标holdout，合计22/8。
- holdout冻结后不依据真实分数改题、提示或Agent；独立验证正确解/错误变体属于建题验收，须记录。首次真实运行后仍如实记录接触与调参，不承诺训练无污染。
- v3中`rc-provider-transient`保留ID以追溯，但移到single_file、faults=["none"]、仅考察increment正确实现；移除recovery维度与Provider重试题面。原计数契约仍由旧版本和已有故障测试保留，本包不实现真实传输故障。
- 最终类别：single_file=8、cross_file=11、recovery=4、memory=4、delivery=3，共30。没有quality safety题；安全工程契约在旧11题中单列，不伪造均衡类别。

## 3. 现有19题逐题优化

业务接口沿用旧fixture；下表是要公开的输入域/行为和隐藏检查重点。表中未定义的行为不加入隐藏断言；不能利用优化顺便提高或改变业务难度。

| ID | 公开需求与验收重点 |
| --- | --- |
| sf-subtract | 整数a-b；正、负、零、相同值、大整数；拒绝加法/绝对差/固定答案。 |
| sf-tail-boundary | count是非负整数；0为空、超过长度返回全部、保序；输入列表不改。负count不评分。 |
| sf-empty-input | 数字列表空时0.0，否则真实平均值；混合正负、小数、单元素；不改输入。 |
| sf-unique-stable | 整数列表保留首次出现顺序；空、全重复、非排序输入；不改输入，不强加hash不可用元素支持。 |
| sf-negative-boundary | 整数负数变0，0与正数原值；避免只测一个负例或只看函数名。 |
| cf-price-discount | 非负price、0—100范围百分比；原价/半价/全折扣与小数，用合理浮点近似；同时测checkout入口与pricing。不增加货币舍入要求。 |
| cf-parameter-validation | 输入为整数且不含bool；1—100有效，其余无效；0/1/100/101/负数及service入口；不强加字符串转换。 |
| cf-module-interface | api.display(first,last)按单空格连接并title；formatter调用契约必须与实际api入口一致。v3的allowed_changes明确为`["formatter.py","api.py","tests/*.py"]`，required_changes为`["*.py"]`，要求至少一个根生产文件实际变化，不强制两者都改；不能新增同名class冒充模块。 |
| cf-update-caller | calculator.multiply已存在且只读；只修report调用，正负/零与格式回归；检测改错文件/绕过实际入口。 |
| cf-backward-api | 保留to_slug和旧slugify，二者小写并将连续空白连接为单连字符；空字符串，client.route入口；接受包装/别名等合法实现。 |
| rc-read-once | 合法有符号十进制整数文本，可有首尾空白；模拟首次read_file失败后继续，不改变parse语义；拒绝恒0及负号丢失。非法文本不评分。 |
| rc-invalid-args | 字符串首尾去空白、小写，内部空白保留，空白串为空；首次写操作的错误是注入信号，不表示模型实参确实错误。 |
| rc-provider-transient | 改为纯整数increment任务，任何整数加1；无故障注入、不提供虚假的恢复分数。迁移理由明确记入matrix。 |
| rc-search-truncated | ASCII字符串前缀匹配忽略大小写，结果保留原拼写、顺序、重复项；空前缀返回全部、空列表为空。故障为首次匹配读取/搜索/glob工具返回模拟OUTPUT_LIMIT；不要求实现业务截断、计数器或“第一次少返回”。 |
| rc-patch-conflict | old为非空字符串，替换所有出现，new可为空，无匹配不变；模拟首次写失败独立于业务，不要求为特定工具重试次数加业务逻辑。 |
| mm-early-constraint | 首轮只确认早期约束，后轮按已有步骤完成render；传入颜色必须使用，text保留；不得把后轮消息拼进首轮或靠硬编码颜色过关。 |
| mm-latest-correction | 保留先初始决定后修正的时序；latest含空串也优先；用不同initial/latest组合，拒绝固定颜色和`latest or initial`。 |
| mm-todo-once | 字符串列表排除completed且稳定去重，空集/全完成/非相邻重复；不改输入与completed；不把业务去重当成Agent实际“不重复执行”的证明。 |
| dl-regression | b为0返回None，其余a/b；原正常除法回归、负数、0分子；拒绝恒None或整数除法。 |

### 3.1 公共交付说明与公开测试

所有coding user_turn公开：非交互、业务需求、同源允许修改路径、可自行补测试、按当前验证要求验证后明确finish。首轮纯确认仅确认，不要求修改/测试；不在首轮泄露后续指令。

每题workspace新增小型`README.md`、`tests/__init__.py`和至少一项可发现的`tests/test_public.py`。公开测试覆盖常规行为与运行方式，不枚举全部隐藏边界；README建议从根运行`python -m unittest discover -s tests -v`。测试内容必须与该turn已公开信息一致：memory首轮可见文件不能透露后轮修正或验收答案，memory公开测试只包含两轮都成立的接口/基线约束，最终修正仅由后轮题面和隐藏验收约束。

修改范围明确列生产文件+`tests/*.py`，使用workspace_root；README与未授权生产文件只读。新fixture已经含tests目录，不用依靠Agent补包初始化。现有`tests`模式可在迁移时收紧为真实文件模式，但必须以实际matcher和真实评分负例证明；不增加`**`。

注意：现有required_changes是“每一个模式都必须命中至少一个修改”，不是模式之间OR。多文件可替代修复不能直接把两条文件路径列为required，否则会误拒只需改单文件的合法解。`*.py`在workspace_root下不匹配`tests/test_public.py`；与严格allowed列表共同使用，并补实际评分验证。

## 4. 新增11道任务的确定契约

所有新题faults=["none"]。除多轮修正题外均一个user_turn。各题README写明以下契约；隐藏测试不得补加未知约束。输入均合成、在内存或隔离临时目录内，无真实外网。

| 新ID / 分类 / split | workspace生产文件及接口 | 公开行为与验收 |
| --- | --- | --- |
| sf-csv-records / single_file / dev | `csv_records.py: parse_records(text: str) -> list[list[str]]` | 合法CSV，逗号分隔、双引号引用/双写转义、CRLF/LF、引号内换行、空字段；与标准库csv.reader默认excel方言一致，空文档[]。验证不是split逗号/逐物理行；不要求容错非法CSV。 |
| sf-aware-time-order / single_file / dev | `time_order.py: sort_events(events: list[dict]) -> list[dict]` | 每项timestamp为带Z或±HH:MM偏移的合法ISO8601时间；按UTC时刻升序，相同时稳定，空列表可用；不改输入或记录内容；拒绝按字符串排序/忽略时区。 |
| cf-config-precedence / cross_file / dev | `settings.py: resolve(defaults, file_values, cli_values) -> dict`；`app.py: build_settings(file_values, cli_values) -> dict` | defaults < file < cli，只有缺键才回退；0/False/空串/None均是显式值；不修改输入；app使用fixture公开默认值。测试直接函数与app入口，拒绝truthiness回退。 |
| cf-cli-result-contract / cross_file / holdout | `cli.py: main(argv: list[str]) -> int`及模块入口；`arithmetic.py: add(a: int,b: int) -> int` | CLI两个必需整数参数；成功stdout一行十进制和、stderr空、exit0；参数缺失/非法exit2、stdout空、stderr含非空诊断，不固定平台语言；支持负整数。算术模块只读。隐藏测试真实子进程验证python cli.py，另测main返回值；拒绝只改返回不改入口退出码。 |
| cf-record-schema-compat / cross_file / holdout | `records.py: normalize(record: dict) -> dict`；`loader.py: load_records(text: str) -> list[dict]` | JSON列表；v1为id/full_name，v2为id/name；输出仅id/name；同时有name和full_name时name优先，即使为空；缺两者抛ValueError；id保持，额外字段丢弃，不改输入。公开允许输入字段类型为字符串；拒绝只支持新格式/假值回退。 |
| cf-page-merge / cross_file / holdout | `pagination.py: merge_pages(pages: list[list[dict]]) -> list[dict]`；`service.py: collect(pages) -> list[dict]` | 按页/页内顺序，以整数id稳定去重，第一条完整记录胜出；空页不结束，空输入[]；不修改页或记录。没有HTTP/cursor服务；拒绝遇空页停止和最后一条覆盖。 |
| cf-package-api-migration / cross_file / holdout | `textkit/core.py: normalize_label(text: str) -> str`；`textkit/__init__.py`；`report.py: label_line(text: str) -> str` | core已实现strip+lower且只读；包导出新normalize_label及兼容旧clean_label，report使用新名输出`label=<结果>`。必须修实际导入链，两个入口结果一致，空串可用；不只测试孤立core。 |
| dl-jsonl-line-diagnostics / delivery / holdout | `jsonl_reader.py: parse_lines(text: str) -> tuple[list[dict],list[int]]`；`cli.py: main(argv) -> int`及模块入口 | 空白物理行跳过但占行号；合法JSON对象保序收集；无效JSON及合法非对象值记1基行号并继续；CLI读一个UTF-8文件，stdout单行JSON对象含records/error_lines，存在错误exit1，否则0，不写输入文件。不测试文件不存在等额外错误策略。 |
| mm-partial-revision / memory / holdout | `formatter.py: render(text: str, prefix: str) -> str` | 第1轮实际实现prefix+text.strip()，保留大小写；第2轮要求正文再lower，同时撤回prefix功能并保留参数签名和strip行为。首轮不得看到第2轮要求；最终对不同prefix验证其被忽略，且strip/lower均保留；两turn成功取AND。最终verifier只证明最终业务，不能单独证明首轮实现正确。 |
| cf-cache-copy-isolation / cross_file / holdout | `cache.py: RecordCache`，`put(key: str,value: dict) -> None`、`get(key: str) -> dict`；`service.py: load_record(cache,key) -> dict` | value为JSON兼容可嵌套dict/list；put后修改原值不污染缓存，get返回值修改不污染下次get，不同key独立；未知key抛KeyError。service调用get；拒绝浅拷贝/直接返回内部引用/恒定结果。 |
| dl-order-summary-pipeline / delivery / holdout | `reader.py: read_orders(text: str) -> list[dict]`；`totals.py: summarize(rows) -> dict[str,int]`；`pipeline.py: build_report(text: str) -> dict[str,int]` | CSV固定表头customer,amount,status；customer按CSV保留，amount为可带负号的整数字符串；只统计status恰为paid，按customer累加整数分，重复客户合并，空数据{}，非paid忽略；customer可含带引号逗号。不要求货币浮点/日期/网络；跨层端到端与各模块回归都测。 |

难度登记：前两题basic；其余advanced；旧19题注明micro/regression，不能把30题等价于30个真实大型仓库issue。每题写一个明确初始缺陷，正确修复应在16轮预算内有合理完成路径；这是设计约束，不保证真实模型成功。

## 5. 评分与独立验收规则

1. 每题隐藏验证至少覆盖正常、边界、回归三个可说明的行为组；断言依赖实际执行结果，不靠源码包含某词或固定实现结构。
2. 初始fixture的隐藏验证必须失败；正确参考修改必须通过公开与隐藏测试；至少两种不同错误变体必须被隐藏验证拒绝。错误变体是从正确解引入不同缺陷，不仅重复初始bug。
3. 30题至少120个“题×变体”隐藏验收：初始错误、正确、错误A、错误B。保存每项退出码/测试数/分类；不能把subTest数当unittest顶层test数混报。
4. 避免评分器过拟合：至少为CSV、稳定去重、旧API兼容三题另备一种不同但合法实现，均应通过。参考代码只在tests侧，不复制给Agent。
5. 使用真实`load_suite`、workspace复制、`install_verifier`、`_run_verification`和评分入口；不能只import隐藏测试直接调用并宣称隔离评分通过。
6. 公开测试必须在标准发现命令下实际运行至少一项。正确解时全通过；初始fixture是否通过公开测试取决于暴露的常规例子，不能以“初始公开全绿”冒充缺陷被测到。
7. 评分负例另覆盖：只改tests、改越界文件、嵌套同名tests路径、产物正确但不finish、多turn前轮失败后轮成功。保留原AND和范围策略。
8. 使用实际消息构造/执行入口确认模型看不到verifier、参考解、错误变体和后轮消息；只检查题面字符串不足以证明隔离。
9. 新版memory只测短多轮，不证明持久化、压缩或无跨会话泄漏；故障题只证明模拟信号后的恢复，不证明真实HTTP、真正参数修复或真实大输出截断。

## 6. 文件职责

- 新增`tests/quality_v3_support.py`：仅供测试的正确修改、错误变体、需求映射辅助与隔离运行；不进入fixture。
- 新增`tests/test_eval_quality_v3_suite.py`：版本/数量/split/类别、fixture资源边界、scope、实验30/90/22/8、历史hash保全。
- 新增`tests/test_eval_quality_v3_verifiers.py`：120+变体行为验收及不同合法解；子进程分别运行，避免import模块缓存串题。
- 新增`tests/test_eval_quality_v3_contract.py`：公开测试发现、消息/隐藏隔离、真实评分负例、多turn与故障边界。
- 修改README.md的Eval段、project.md、reviewer交接；新增实施报告`docs/reviews/2026-10-20-quality-v3-implementation.md`。
- 本计划与提示词保留为审查依据。不要改旧计划/旧真实报告来“同步新成绩”。

## 7. 实施任务

### Task 1：版本、保全和新套件骨架

**Files：** 新suite、matrix、migration、四个实验清单、suite测试；旧文件只读。
**Interfaces：** 沿用`load_suite(path)`、`load_experiment(path)`返回结构，不新增生产接口。

- [ ] 在`runtime/quality-v3-expansion-20261020/implementation-<唯一后缀>/`记录实际before、tracked/untracked状态与白名单文件hash。覆盖src/tests/scripts/evals及将改文档；不递归复制runtime或未知私密目录。
- [ ] 单独保存旧suite全文件hash与最新两次原生真实结果清单hash；保全旧审查证据。快照不能仅引用HEAD。
- [ ] 先写30quality/0contract、22dev/8holdout、类别8/11/4/4/3、预算16、故障迁移的失败测试，保存RED。
- [ ] 建立新骨架、复制19题受控fixture、登记新11题，不复制旧README中的过期“尚未真实运行”段落。
- [ ] 实现四个实验清单并验证planned_trials=30/90/22/8；contract不入分母；GREEN后记录数据边界。

### Task 2：旧19题优化与评分器自检

**Files：** 新suite中旧19题、测试support、verifier测试。
**Interfaces：** 沿用旧19个业务接口，矩阵requirements→公开/隐藏测试映射。

- [ ] 先针对第3节边界写错误变体，证明旧隐藏测试不足处，不能为每题虚构RED；已有覆盖处记录“已有覆盖”。
- [ ] 逐题补公开契约、可发现公开测试和隐藏边界，保持合理替代实现可通过。
- [ ] 修正截断题的工具层描述与业务层隔离；Provider计数题在v3移出恢复分类/故障，旧定义不变。
- [ ] 在真实隔离复制中验证19×4=76项基础变体，连同追加合法解；记录每项测试数与退出码，不能只检查总进程exit0。
- [ ] 明确多轮首轮可见内容，确认最新修正不提前泄露；GREEN后更新migration。

### Task 3：新增11题与工程路径验证

**Files：** 第4节11题workspace/verifier/case、support与verifier测试。
**Interfaces：** 第4节固定的文件、函数/类及返回类型；不新增外部依赖或生产工具能力。

- [ ] 分批实现前3题、随后4题、最后4题；各批先写正确/错误变体验收，再加入fixture及隐藏验证，保留RED/GREEN。
- [ ] 每题独立执行初始/正确/两种错误变体，共44项基础变体，验证完整行为而非函数名称。
- [ ] CLI测试执行真实子进程并检查stdout/stderr/exit；包迁移测试实际导入；cache测试嵌套对象；pipeline测试带引号客户名和回归。
- [ ] 新多轮题用当前Runtime公开路径验证两轮顺序、正常收尾与AND；不依赖纯最终verifier推断首轮业务正确。
- [ ] 冻结holdout并记录建题接触：未跑真实模型、已作人工与脚本验收，不称其对作者不可见。

### Task 4：评分隔离、全量验证与交接

**Files：** contract测试、README、实施报告、project与handoff。
**Interfaces：** 继续使用现有评分/消息构造流程，所有旧结果读取语义保持。

- [ ] 先补第5节范围/只改测试/不finish/多turn负例及消息隔离测试，保存明确失败原因；不靠字符串断言代替执行。
- [ ] 新30题离线验收通过，4份实验dry-run均正确；dry-run不得加载Key、实例化真实Provider或联网。
- [ ] 跑全部`test_eval*.py`及完整unittest discovery，记录通过/失败/skip与时长；Windows符号链接或平台skip不能算通过。
- [ ] 只读语法检查新增Python；检查tracked及untracked文件空白问题。对照actual before生成after/delta/hash，复核旧文件、src与旧成绩未改。
- [ ] 报告逐题覆盖、120+变体、旧/新版本不可严格横比、已知边界与未验证范围；更新交接为“实现完成，待独立复审”，随后停止。

如果发现必须修改生产runner/schema才能完成，先提供具体反例和最小必要变更说明，交回reviewer；不能扩大到Agent重构、真实故障注入或默认权限调整。本计划已按现有能力设计，通常不需要这些改动。

## 8. 命令与证据

在项目根，使用现有`.venv/Scripts/python.exe`；测试需`PYTHONPATH=src;tests`时仅设当前进程环境。每次日志用新的文件名，不覆盖失败记录。

```powershell
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p "test_eval_quality_v3*.py" -v
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p "test_eval*.py" -v
.venv/Scripts/python.exe -X utf8 -B -m tricoder eval --experiment evals/quality-v3/experiment-quality-baseline.toml --dry-run --no-color
.venv/Scripts/python.exe -X utf8 -B -m tricoder eval --experiment evals/quality-v3/experiment-quality-repeat3.toml --dry-run --no-color
.venv/Scripts/python.exe -X utf8 -B -m tricoder eval --experiment evals/quality-v3/experiment-quality-dev.toml --dry-run --no-color
.venv/Scripts/python.exe -X utf8 -B -m tricoder eval --experiment evals/quality-v3/experiment-quality-holdout.toml --dry-run --no-color
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -v
git diff --check
```

预期：新套件专项、全部eval、全量exit0；四次dry-run分别30/90/22/8；skip必须列明。测试总数由本次实际输出记录，不把旧1838复制成新结果。

`git diff --check`不覆盖全部未跟踪文件，须另对白名单新增文件检查；证据目录至少含before/after清单、delta、新文件索引、RED/GREEN日志、逐题变体验收JSON、dry-run日志、全量日志、保全结果和SHA256索引。无效初始测试/脚本失败也保留并解释。

## 9. 独立复审交付标准

- [ ] 30题全部真实质量定义，原19来源可追溯，新11语义与此文一致；旧11contract独立保留。
- [ ] 需求与隐藏断言逐项对应，无未公开条件、首轮泄漏或假故障能力宣传。
- [ ] 初始失败、正确通过、两个不同错误变体拒绝，至少120项实际运行；至少3题替代合法解通过。
- [ ] 未篡改scope/AND/finish、src、默认权限、默认预算与历史日志；实际before/after可核对。
- [ ] 全量与dry-run证据完整，所有skip和未验证范围写明。
- [ ] 新题库尚未真实调用，因此不宣称模型能力提升；等待独立复审后由用户另行安排新30题真实基线。
