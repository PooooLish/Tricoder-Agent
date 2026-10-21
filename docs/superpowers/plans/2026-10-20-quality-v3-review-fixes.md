# Quality V3 评分器复审修复 Implementation Plan

> **For agentic workers:** 使用 `superpowers:executing-plans` 按Task1→2→3→4实施，单会话执行，不派生子Agent、不提交。用户已选择将修复交给coding session，本文件不要求重新选择实施方式。

**Goal：** 修复Q3-R1/R2两个评分漏判，以及Q3-R3对合法实现的误拒，为30题真实模型复测提供可靠的离线前置验收。
**Architecture：** 只修改四份现有隐藏verifier，在测试侧增加真实隔离评分正反例。沿用现有loader、runner、评分和Runtime，公开业务要求与Agent行为不变。
**Tech Stack：** 当前Python 3.11+、标准库unittest/mock/subprocess、现有Eval隔离与证据工具；不增加依赖。
**Spec：** 本文第1—4节为修复契约，第5—7节为实施与验收。事实依据是 `docs/reviews/2026-10-20-quality-v3-rereview.md` 及其中独立反例。
**状态：** 待coding实施。Q3-R1/R2/R3仍未关闭，不能用本文代替修复或复审通过。

## Global Constraints

- 工作区仅 `D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli`；遵守工作区与项目AGENTS.md，先读README、pyproject、project和reviewer交接。
- 保留脏工作树和所有未跟踪内容。HEAD不是before基线，不reset/clean，不覆盖旧日志，不回写历史成绩。
- 不改`src/`、Agent/工具提示、权限、命令策略、finish/验证/清理/取消/owner/AND或默认预算。本包问题是验收脚本错误。
- 不改任何case.toml、workspace、suite.toml、实验清单、题目数量/split/预算/公开需求；不把“只能调用一次”等隐藏限制补进题面来消除误拒。
- 旧v1/v2/v2.2/regression/smoke及原审查、实施证据保持不变。保留当前V3四份verifier实际before和完整哈希。
- V3尚无真实模型成绩，本包作为未验收版本的修正，仍使用`quality-v3`目录与版本；before/after verifier指纹明确分开，不重解释旧离线结果。
- 不读取`.env.local`、真实密钥或用户数据库，不安装、不调用真实模型、不提交/推送。真实调用留给修复后的独立复审会话，通过后执行用户已授权的30题基线。

## Review Focus

1. 返回id正确却丢失timestamp/嵌套字段：必须拒绝，同时允许等值副本而非只接受对象身份相同。
2. 主程序拒绝多余参数但main接受，或相反：直接main与真实子进程均验证参数错误。
3. 用删掉spy/调用检查消除误拒：绕过get或包新导出的实现仍须拒绝。
4. 改成另一种隐含实现约束：多次调用、关键字传参、slots与合法输入预处理不能误拒。
5. 用原reviewer探针exit码代替修复验收：原探针断言的是旧缺陷，修复后必须使用新期望，保留原脚本与结果。

## 1. 已复现的缺陷与执行证据

独立审查时全部Eval 309项通过307项、跳过2项；已有125个变体按预期。以下新反例仍暴露评分错误，因此不能仅重跑原测试声称修复。

证据根：`runtime/quality-v3-expansion-20261020/review-20261020/`。

| 发现 | 修改位置 | reviewer证据标签与当前错误行为 |
| --- | --- | --- |
| Q3-R1/P2 | `evals/quality-v3/cases/sf-aware-time-order/verifier/test_hidden.py` 第8、14、20行 | `time-drops-record-fields`正确排序后只返回id，隐藏通过；`time-control`正常通过。 |
| Q3-R2/P2 | `evals/quality-v3/cases/cf-cli-result-contract/verifier/test_hidden.py` 第12—14行 | `cli-ignores-extra-arguments`使用len(argv)<2拒绝缺参数，却忽略第三个参数，隐藏通过；`cli-control`正常通过。 |
| Q3-R3/P2 | `evals/quality-v3/cases/cf-cache-copy-isolation/verifier/test_hidden.py:27`、`cf-package-api-migration/verifier/test_hidden.py:22` | `cache-valid-two-get-calls`、`package-valid-two-export-calls`业务正确，因assert_called_once_with失败；一次调用对照通过。 |

先阅读`probe_scoring.py`、`probe-results.json`和`probes/*/reviewer-result.json`。这些探针使用真实隔离workspace/隐藏测试/评分，但合成了Agent完成状态，目的只在验证评分器；不能作为真实模型或新Agent质量成绩。

原`probe_scoring.py`明确断言旧状态：两种错误passed、两种合法failed。不能在修复后原样运行它，看到exit1就误判修复失败；也不能修改原脚本来抹去证据。应在本轮新证据目录或新测试文件中重建相同实现、改用下文正确期望。

## 2. Q3-R1：完整记录而非id投影

### 修改契约

- 比较排序后完整记录列表，至少包括timestamp、id及嵌套dict/list业务字段；不能只比较id序列。
- expected必须在调用candidate之前从输入独立复制，不能在candidate可能修改输入后计算expected。
- 继续验证UTC时刻排序、同一时刻不同偏移的稳定顺序、空列表和输入不变。
- 结果允许原记录引用、浅拷贝或深拷贝，只要内容与公开契约相符；不得添加`is`或id()身份约束。
- 输入只要求timestamp字段，id不是业务前置条件；至少一个正常例子不含id，以防评分器把辅助标签变成隐藏输入要求。

### 必须新增的评分回归

通过实际`run_isolated_case`等既有入口执行，隐藏子进程exit1、failure_codes包含verification_failed才算拒绝，不能把导入错误或基础设施异常当成有效拒绝。

| 变体 | 修复后期望 |
| --- | --- |
| reviewer的只保留id实现 | failed；完整业务oracle仍显示字段丢失 |
| 额外错误：只移除/改变嵌套业务字段、id和timestamp完整 | failed |
| 正常返回排序后原记录列表 | passed |
| 返回深拷贝后的完整排序记录 | passed；不能因身份不同失败 |

将这些场景扩充进现有normal/boundary/regression三组，不必新增隐藏测试方法名，避免无关地改变矩阵结构及现有方法名契约。

## 3. Q3-R2：完整验证参数数量

### 修改契约

- 两个合法整数参数成功；恰好两个是已有公开条件，三个/四个参数都应拒绝。
- 直接调用`main(argv)`：返回整数2，不抛SystemExit；stdout为空，stderr有非空诊断，不要求固定语言或具体文本。
- 实际`python cli.py ...`：退出2，stdout为空，stderr有非空诊断。
- 保留零参数、一个参数、非法整数、负整数及正常输出回归。
- 捕获stdout/stderr必须每个输入独立初始化，不能由上一例的输出让下一例误通过。

### 必须新增的评分回归

| 变体 | 修复后期望 |
| --- | --- |
| reviewer的忽略第三参数实现 | failed |
| main忽略多余参数、模块入口额外检查后正确拒绝 | failed；证明直接main路径已测 |
| main正确、模块入口预先截断argv[:2] | failed；证明实际CLI路径已测 |
| main与入口都正确 | passed，全部既有正常/错误输入行为保持 |

不要仅给主进程增加一个三参数断言，就遗漏函数入口。新隐藏子进程仍受现有20秒外层验证期限控制，不改生产命令白名单或预算。

## 4. Q3-R3：观察目标调用，不强迫调用次数

### 缓存题

- 保留类级spy及真实`RecordCache.get`执行，兼容slots；不退回给实例临时赋值的spy。
- 目标是确认service确实调用目标实例/目标key的get并返回正确结果；允许一次或多次，不允许零次。
- 可在autospec规范化后用“出现过期望调用”的断言；保留位置参数与`get(key=key)`的合法性，不手写只接受一种参数形态的匹配。
- 完整保留put/get嵌套隔离、不同key、空key、缺key异常和service业务检查。
- 原`test_cache_service_cannot_bypass_public_get`仍必须拒绝直接读取内部storage的实现，不能删除该负例。

### 包迁移题

- 继续证明report使用包上的新导出，绕过新导出直接调用core/旧别名的实现仍拒绝。
- 接受一次或多次调用；如spy替代返回值会改变合法控制流程，优先用包装真实函数的观察方式，使业务语义保持。
- 不把“原始字符串必须原样传入”变成新约束；例如先`text.strip()`再交给strip+lower的normalize_label是等价实现，公开契约没有禁止。检查目标被使用及最终结果，不无故固定内部参数表达方式。
- 保留直接导出、新旧名称、report实际导入链、空串及正常结果验证；重新加载模块后的patch恢复应在finally或可靠上下文中完成，不能污染其他测试。

### 必须新增/保留的评分回归

| 变体 | 修复后期望 |
| --- | --- |
| reviewer的两次get合法实现 | passed |
| 普通一次get、slots缓存、关键字get调用 | 全部passed |
| service直接读取内部数据而不调用get | failed |
| reviewer的两次包新导出合法实现 | passed |
| report先strip再调用新导出、结果正确 | passed |
| 包正常一次新导出调用 | passed |
| report只调用core或旧别名、没有调用包新导出 | failed |

不通过要求精确实现结构或只接受参考代码防作弊。调用观察只证明观测到的接口使用及对应行为，不宣称能证明任意程序的完整数据流。

## 5. 文件范围与实施任务

允许修改：

- 第1节四份`verifier/test_hidden.py`。
- 新增`tests/test_eval_quality_v3_review_fixes.py`，存放本包评分正反例，复用`quality_v3_support.isolated`与测试侧参考实现。
- `evals/quality-v3/migration.md`、`README.md`更新修复记录；`case-matrix.json`仅在确实需要补充对应需求—断言映射时修改，结构保持。
- 新增`docs/reviews/2026-10-20-quality-v3-review-fixes-implementation.md`；更新project.md与reviewer交接。

默认不改已有125项变体、测试helper及旧suite测试。本包新测试独立追加，不能替换旧负例来维持通过数。所有新的isolated标签唯一；日志目录每次运行独立，避免`open("x")`冲突。

### Task 1：保全与Q3-R1

**Interfaces：** `quality_v3_support.cases()`、`isolated(case,label,files,public=True)`；实际结果含status/failure_codes/verification与进程收据。

- [ ] 创建`runtime/quality-v3-review-fixes-20261020/implementation-<唯一后缀>/`，保存actual before、文件清单及hash。
- [ ] 固定src、全部case.toml/workspace、suite/实验、旧版本与已有审查/实施证据hash；不要复制未知私密目录。
- [ ] 新评分测试先按正确期望断言id-only错误被拒，保存有效RED；补完整输出与嵌套字段验收。
- [ ] 加入深拷贝合法解、无id输入；隔离评分GREEN后保存逐项收据。

### Task 2：Q3-R2

**Interfaces：** 真实CLI子进程和`main(argv)->int`，各自独立输出捕获。

- [ ] 先加入忽略多余参数、main错误/入口正确、main正确/入口错误三个负例，保存有效RED。
- [ ] 补齐三参数/四参数的双入口检查，保持既有三组隐藏方法。
- [ ] 正常实现及原参数边界全部GREEN，记录实际返回值和输出结果。

### Task 3：Q3-R3

**Interfaces：** 类级get观察和包新导出观察；只替换测试探针，不改业务fixture。

- [ ] 先加入两次get、两次新导出合法解，断言passed并保存被误拒的RED。
- [ ] 放宽无依据的次数/内部表达约束，保持真实业务检查和目标调用观察。
- [ ] 验证slots、关键字、预strip等合法实现，以及绕过目标调用的负例；不能只把assert_called_once_with删掉。
- [ ] 逐项收据证明合法接受、错误拒绝，未改变原作用域/AND评分。

### Task 4：完整离线验收与交接

- [ ] 新修复测试、所有V3测试、全部Eval及完整unittest discovery分别运行；不合并重复执行数当作独立覆盖。
- [ ] 四份CLI dry-run仍为30/90/22/8。测试总数、变体总数按实际结果报告，不能硬编码旧309/137/125。
- [ ] 核对原125项仍按预期，加上本包新增正反例；使用新脚本重放reviewer8例，目标为2个业务错误failed、6个正确/等价实现passed。
- [ ] 只读语法、tracked/untracked空白与hash检查；确认只有白名单文件变化，保留所有旧日志。
- [ ] 更新实施报告、migration与交接，写“实现完成，待独立复审”；保留原暂不通过报告和Q3-R1/R2/R3未关闭状态，随后停止。

## 6. 验证命令与证据

仅当前测试子进程使用`PYTHONPATH=src;tests`、`PYTHONDONTWRITEBYTECODE=1`，`TRICODER_V3_EVIDENCE`指向本次运行独占新目录，不指向旧reviewer或实施目录。

```powershell
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p "test_eval_quality_v3_review_fixes.py" -v
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p "test_eval_quality_v3*.py" -v
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p "test_eval*.py" -v
.venv/Scripts/python.exe -X utf8 -B -m tricoder eval --experiment evals/quality-v3/experiment-quality-baseline.toml --dry-run --no-color
.venv/Scripts/python.exe -X utf8 -B -m tricoder eval --experiment evals/quality-v3/experiment-quality-repeat3.toml --dry-run --no-color
.venv/Scripts/python.exe -X utf8 -B -m tricoder eval --experiment evals/quality-v3/experiment-quality-dev.toml --dry-run --no-color
.venv/Scripts/python.exe -X utf8 -B -m tricoder eval --experiment evals/quality-v3/experiment-quality-holdout.toml --dry-run --no-color
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -v
git diff --check
```

新测试不得跳过；旧平台/符号链接skip单列且不计通过。每轮日志和变体收据使用新目录，保存命令、退出码、耗时、实际测试数、modified_files与失败分类。git diff不覆盖未跟踪文件，另检查新增测试文件。

## 7. 完成标准与真实复测衔接

- Q3-R1字段丢失与嵌套字段破坏均拒绝；完整原记录与完整副本均接受。
- Q3-R2双入口拒绝多余参数，原正常/缺失/非法/负整数行为保持。
- Q3-R3至少一次目标调用被验证，但不强迫一次；合法多次/slots/关键字/预处理通过，绕过目标调用仍拒绝。
- 原125项及新评分测试、全量、dry-run完成；全部旧source/fixture/题面/配置/证据保全。
- 独立reviewer复审通过前不运行模型。用户已授权通过后的DeepSeek deepseek-v4-flash、30题各1次、16轮、memory off/off；实施会话不提前消费这项条件授权。
- 真实复测属于验证新V3整体质量，不能与v2.2分数直接归因比较，也不能用新离线成绩推断真实模型成功率。
