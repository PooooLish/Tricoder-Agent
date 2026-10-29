# 真实复测交付效率与验证指引修复方案

> **For agentic workers:** 使用 `superpowers:executing-plans` 在接收本文件的 coding session 分项实施；本 reviewer 会话仅交付文档。步骤中的检查框用于实施记录。不得自动提交、推送或调用真实模型。

**Goal:** 在不放宽权限、验证证据或评分规则的前提下，减少写入工具纠错和过早 finish，改善 16 轮预算内的完整交付。

**Architecture:** 增强现有工具反馈与宿主验证反馈，增加少量追加式轮次提示；不新增自动执行或自动通过能力。将公开验证指引的修订发布为独立 quality-v3.1 题库，保留 quality-v3 和本轮真实结果。

**Tech Stack:** Python 3.11+、现有 CodingAgent / SessionRuntime / ToolRegistry、unittest、TOML Eval 定义；不新增依赖。

**Spec:** 本文件第 1–4 节为设计与范围，第 5–8 节为实施和验收；依据 `docs/reviews/2026-10-27-quality-v3-repeat3-real-rerun.md`。这是 reviewer 提出的下一轮方案，尚未实施、尚未证明真实模型收益。

## 1. 已知事实与根因边界

基线 run：`runtime/evals/20261027t055018.826595z/`。DeepSeek deepseek-v4-flash，30 题各 3 次、16 轮/user_turn、16000 上下文、memory off/off。90 次全部完成：产物 90/90，完整交付 87/90；完整 pass@1/2/3 为 96.6667% / 98.8889% / 100%，pass^1/2/3 为 96.6667% / 94.4444% / 93.3333%。main token 4,577,776，整题平均 32.014 秒，P95 53.889 秒。

| 实例 | 可确认的轨迹 | 本轮要处理的问题 |
| --- | --- | --- |
| ordinal 36，sf-aware-time-order，第 2 次 | 创建已有文件、三次 patch_count_mismatch、另一次补丁参数错误；两次测试失败后改自建测试；第 16 轮测试成功，没调用 finish | 工具纠错消耗、缺乏交付预算安排 |
| ordinal 66，同题第 3 次 | 两次补丁计数错误、两次测试失败；第 14 轮测试成功，第 15 轮 finish 因 time_order.py pending 被纠正，第 16 轮只读取文件 | 验证覆盖反馈没有转化成正确下一步；finish 太晚 |
| ordinal 82，rc-search-truncated，第 3 次 | 合成输出截断、参数错误、创建已有文件、两次非法 hunk；第 15 轮 finish 因 searcher.py pending 被纠正，第 16 轮 compileall 成功，没机会再次 finish | 同类工具错误、补验证和交付没有预留轮次 |

全体样本有 51 次 patch_count_mismatch、43 次 file_already_exists、47 次 missing_relevant_verification 纠正，其中 45 次最后交付成功。中途错误次数不是失败试次数。

三个失败产物在独立副本中测试通过；scope/cleanup 均通过。精确历史 argv 和失败测试 stdout 已脱敏，不能补造，也不能宣称完成了原始 Runtime 因果重放。已有 oracle 只证明最终产物和命令分类/覆盖机制；评分不追认。

**不要重复修已有功能：**

- `protocols.py` 已明确 create_file 只创建、小改优先 edit_file、零测试不通过、tests 定向发现不自动覆盖根源码、完成需 finish。
- `tools/write.py` 已提供合法 unified diff 示例及计数诊断，已有文件也返回 read/edit 建议。
- `verification_feedback.py` 已在工具批次后发送 pending_paths，finish 最多纠正一次。不是完全缺少提前反馈。
- `evals/quality-v3/README.md:26` 已解释定向 tests 的覆盖限制，但模型可见的 30 份 `workspace/README.md:7` 首选命令仍是 `python -m unittest discover -s tests -v`，只笼统要求按当前验证要求检查。

因此本轮是提升反馈可执行性和降低操作摩擦，不是重做工具协议、引入新 Replan 框架或宣称发现评分误判。

## 2. 代码定位与因果链

行号对应本方案核查时的工作树；实施前以函数名重新定位，不把 HEAD 当作精确 before。

| 文件/位置 | 当前行为及改动边界 |
| --- | --- |
| `src/tricoder/protocols.py:20`，COMMON_SYSTEM_PROMPT | 现有工具选择、验证、交付规则。补充结构错误后的替代动作和收尾预算原则，保持 native/legacy 共用 |
| `src/tricoder/tools/write.py:125`，EditFileTool | old_text 必须非空且在当前文件中恰好出现一次；说明中应明确，不能发明整文件覆盖参数 |
| `src/tricoder/tools/write.py:246,300,511`，CreateFileTool / _PATCH_FEEDBACK / _patch_feedback | 创建已有文件已安全拒绝；补丁结构错误当前主要建议继续修补丁。为结构错误增加 read/edit 替代建议，不改错误码、解析或写入语义 |
| `src/tricoder/engine/loop.py:222–246,338–345` | round_number/max_rounds 目前供 observer/event 使用；Provider 请求没有专门的剩余轮次提示。usage 锚定实际请求前缀，新增提示必须追加，不能每轮替换旧消息 |
| `src/tricoder/context/manager.py:616,1015` | Provider token 锚依赖前缀精确一致。每轮动态替换 system prompt 或只注入瞬时请求后删除，会使锚失效；本方案不改估算算法 |
| `src/tricoder/context/manager.py:959`；`context/summarizer.py:36` | 当前分组只保留已识别反馈 kind，摘要规则也排除这些反馈。本方案复用 protocol_feedback，不能把新的 JSON type 误用为新 message.kind |
| `src/tricoder/engine/verification_feedback.py:48,103,137,256,282` | 批次/finish 反馈、blocking_checks、reason_code 和 2000 字符裁剪；在真实成功但覆盖仍不足时给具体解释，保留旧失败优先级 |
| `src/tricoder/tools/command.py:53–94` | unittest `-s tests` 的 targets 是 tests；默认根目录 discover 的 targets 是 `.`。这是范围分类，不追踪 import |
| `src/tricoder/workspace/check_coverage.py:24–36`；`engine/completion.py:215` | 仅当前可信快照的有效检查目标并集覆盖本轮修改路径；`tests/` 不能覆盖根源码。本轮不修改这些判定 |
| `src/tricoder/engine/loop.py:565–660` | 完成评估、最多一次恢复、硬预算停止。不增加“免费最后一轮”，不自动 finish |
| `tests/delivery_helpers.py` | 已有临时项目、临时数据库、QueueProvider、真实 SessionRuntime/工具执行，可复用来做宿主验收 |

独立命令分类探针再次确认：对 `time_order.py` 与 `tests/test_public.py`，`-s tests` 留下前者，根目录 discover 的路径覆盖为空 pending。命令成功、覆盖已齐、业务正确是不同事实；本轮保持这种区分。

## 3. 方案选择与边界

推荐 **有界反馈 + 轮次提示 + 新版公开验证指引**。它直接针对观察到的浪费，沿用已有执行与证据机制。

暂不采用以下替代方案：

1. 增加 max_rounds 或默认 relaxed/fullaccess：没有权限拒绝导致这三个失败的证据；增加预算会改变测试条件，不能代替修复。
2. 任意测试成功即视为根目录覆盖、测试成功后自动判定交付：会放松当前可信证据和 finish 契约。
3. 自动重算补丁计数、把 create_file 自动转换成覆盖、自动补跑命令或新增复杂重试状态机：会改变操作含义与审批边界；本轮无必要。

**全局约束：**

- 保留全部未提交和未跟踪内容；在 `runtime/quality-delivery-guidance-fixes-20261027/<unique>/` 保存 actual before、after、delta、hash、RED/GREEN 与最终日志。只对白名单源码、测试、文档、题库取快照；不得包含凭据或用户数据库。
- 不修改 patch 解析严格性、错误恢复级别、重复失败守卫、命令白名单、默认权限、资源所有权、终止期限或 finish 一次纠正上限。
- 不修改 hidden verifier、公开业务断言、allowed/required_changes、task/steps、故障配置、case ID、split、题数、16/16000 预算或四维 AND；已有质量指标计算不改。
- 不读取 `.env.local` 或真实凭据、用户 Session 数据库；不安装依赖、不提交/推送、不调用真实模型。前次 90 次的调用授权已经履行，不延伸为 coding session 的新实验授权。
- 未触及的搜索 P2、Q3-R1/R2/R3 保持原结论；不自动推进 Replan 或其他历史待办。

## 4. 预期行为合同

### 4.1 工具错误反馈

- 补丁 `patch_invalid_header`、`patch_invalid_hunk`、`patch_count_mismatch`、`patch_invalid_format` 的现有固定诊断后，提供固定替代建议：已读且当前内容明确的单文件小改改用 edit_file；old_text 精确且唯一；内容不确定时先 read_file。必要的多文件原子补丁仍可修正后使用。
- 提示不要无变化地重复失败补丁。现有 ProgressGuard 继续负责重复失败停止，不加第二套计数器，不依据模型自报错误次数决定权限或恢复。
- file_already_exists 建议直接使用已有当前读取结果编辑；仅内容未知/已变化时重读，避免无条件增加一次读取。不能靠建议绕过真实内容匹配或审批。
- 原有数字 diagnostics、detail_code、恢复类型、无副作用结论和脱敏边界保持。错误反馈不回显补丁正文、old_text/new_text 或原始路径。
- 不写入题目专用算法、答案或某个 case 名；强调最小满足需求的修改，不能要求模型删测试或为通过而弱化断言。

### 4.2 轮次预算提示

新增纯函数 `build_round_budget_feedback(*, round_number: int, max_rounds: int) -> str | None`，建议放在 `src/tricoder/engine/budget_feedback.py`。

- 当 `round_number == 1` 或 `max_rounds - round_number + 1 <= 3` 时返回固定字段 JSON，其他轮次返回 None；条件重叠只发送一次。正整数且 `1 <= round_number <= max_rounds`，非法内部输入抛 ValueError。
- 字段：`type="execution_budget"`、`round_number`、`max_rounds`、`remaining_including_current`、`remaining_after_current`、有界固定 message。长度不超过 800 字符；无源码、路径、Provider 原文。
- 16 轮示例：第 1/14/15/16 轮发送；第 14 轮的 remaining_including_current=3、remaining_after_current=2。语义明确为本次 user_turn，不是整个 Eval trial、HTTP 重试数或工具数量。
- 提示为必要修改、业务检查、覆盖补齐和显式 finish 安排剩余轮次；不能为省轮次跳过必需测试、强制宣称 completed 或自动延长预算。最后一轮诚实反映本轮之后没有新决策机会，无法交付时允许 incomplete。
- 在 loop 的取消检查之后、normalize_history / _prepare_memory / request_view 之前，将提示通过现有 `_feedback_message(..., "protocol_feedback")` **追加到 state.messages**；仅 JSON 的 type 为 execution_budget，message.kind 复用 protocol_feedback。不替换固定 system prompt、过去提示或用户任务；沿用正常消息序号与任务归属。每次实际动作请求最多新增一条，每个 user_turn 最多四条。
- 不新增 execution_budget 消息 kind：现有 ContextManager 会删除未识别的当前回合消息。复用 kind 后要通过真实 prepare→Provider 链路测试；预算通知也不能被算作 native_missing_tool_responses 或协议纠错失败。结构化摘要沿用“宿主反馈不提炼为目标/约束/决定/待办”的规则，不让旧预算变成后续任务约束；增加跨 turn 摘要边界回归。
- 不把预算字段当作可信验证状态、memory 权限或自动命令。后续 user_turn 从自己的 round 1 重新开始，过去提示只是历史。原 planning phase 不消耗该动作轮数，不增加新请求。
- 不采用瞬时注入后删除：保留正常历史前缀以支持 ContextManager 的 usage 锚。新增 token 如实统计，不能从报告中扣除提示成本。

### 4.3 覆盖不足时的可执行提示

在现有 `build_batch_feedback` / `finish_recovery_feedback` 中复用一个内部纯展示 helper（建议 `_add_next_step_hint(payload, state) -> None`）；不得修改 state、validation、evidence 或扫描文件。

- 当有效成功检查后仍有可信本轮路径未覆盖，且无 unresolved_checks、零测试、unknown、清理失败或过期证据优先问题时，说明“检查成功但范围不足；读取文件不能解除 pending”。沿用现有 reason_code，避免为了文案改变诊断兼容。
- 可加入固定枚举 `next_step_hint="verify_pending_paths"` 与固定 message：按 pending 补认可检查；适用 unittest 项目可从工作区根运行 `python -m unittest discover -v`，或在已有业务测试后补相应路径语法/静态检查。命令只是条件示例，仍须模型选择、策略校验、审批和真实执行；禁止用路径拼接可执行命令。
- unresolved checks 优先输出 `repair_blocking_checks`，保留当前 check_id、计数、同检查复验要求和原 message。新建议不能让成功 compileall 或另一条 root discovery 消除旧失败。
- zero_tests_reported 保留原诊断，提示修正发现范围或补有效测试；不得建议把零测试当成功。
- 状态已 satisfied 时可给 `finish_if_task_complete`，明确“若用户任务也已完成”；宿主检查通过不等于业务任务一定完成。
- pending 路径展示仍用现有受控相对路径、20 项/2000 字符上限。新的提示必须参与有界裁剪，保留有效 JSON、blocking_checks 身份/计数，不提高上限规避问题；已截短路径不得转成命令参数。
- finish 纠正反馈使用相同原则，强调已得到 pending 时继续只读不会补验证；不重放命令，不清除恢复次数。

### 4.4 题库指引版本

新增 `evals/quality-v3.1/`，保留原 `evals/quality-v3/` 的每个文件字节。新 suite `id="quality-v3-1"`，`benchmark_version="quality-v3.1"`；ID 不得含点，`loader.py:32` 只接受小写字母/数字/下划线/连字符。四份 experiment_id 分别采用 `quality-v3-1-baseline/repeat3/dev/holdout`，condition ID 仍为 quality-baseline。

- 以当前 V3 的完整已复审题库为来源，case 定义、生产初始文件、公开测试和 hidden verifier 保持原样；仅改新包 30 份 workspace/README 的操作指引、suite/experiment 身份和配套文档/来源 manifest。
- 首选命令改为：从工作区根执行 `python -m unittest discover -v`。先通过全库离线发现/Runtime 检查证明它确实运行公开测试，不能机械换字符串后把“exit0、Ran 0 tests”当成功。
- 说明测试通过后查看宿主 pending/blocking；检查后再次修改必须重验；业务测试不能用 compileall 替代；确认轮不要求无关测试；最后明确 finish。可以定向排错，但不推荐仅靠 `-s tests` 交付。
- 不把全库顶层 README 当成模型必然已读的输入；必要说明写进每题 workspace/README。不得泄漏 hidden verifier、参考解、后续 memory 修正或历史失败答案。
- 新增 `source-v3-sha256.json`，覆盖旧 V3 每个受版本管理的普通题库文件。记录新包相对旧包的逐文件差异白名单；继承的业务矩阵保持要求/验证映射不变，并在新顶层 README 解释继承关系。
- 30 quality / 0 contract、22 dev / 8 holdout、四清单 30/90/22/8、重复次数、max_trials、总时限、seed23、model、memory、faults、16/16000 均保持。
- 新旧不同 benchmark 不混分母、不覆盖旧 run、不解除 eval-compare 的题库差异拒绝。公开指引变化影响任务输入，不能把新版收益全部归因于 Agent。

## 5. 分项实施

### Task A：工具反馈的具体替代动作

**Modify:** `src/tricoder/tools/write.py`、`src/tricoder/protocols.py`。
**Test:** 扩展 `tests/test_patch_feedback.py`、`tests/test_command_compatibility.py` 中已有 native/legacy 指引测试；需要整段链路时新增 `tests/test_delivery_guidance.py`。
**Interfaces:** 复用 ToolResult、detail_code、_patch_feedback；不改工具 schema、解析器或恢复规则。

- [ ] 为结构错误后 read/edit 替代建议、已有文件无需盲目重读、唯一 old_text 写 RED 测试。断言实际 Provider 可见反馈与语义，不只匹配一段无执行效应的字符串。
- [ ] 最小更新文案；既有安全反馈、合法补丁示例、纯数字 diagnostics 保留。
- [ ] 用 fake Provider + 真实工具执行“错误补丁 → edit_file → 真实检查 → finish”，native/legacy 均成功；错误补丁阶段无审批/无修改，后续编辑仍经审批。加入 policy_denied、unknown effects、审批拒绝不能继续写入的负例。
- [ ] 运行专项测试，保存 RED/GREEN 和新增用例说明。不能声称 fake Provider 会主动学会策略；它只证明替代链路和反馈可用。

### Task B：预算及验证反馈

**Create:** `src/tricoder/engine/budget_feedback.py`、`tests/test_round_budget_feedback.py`。
**Modify:** `src/tricoder/engine/loop.py`、`src/tricoder/engine/verification_feedback.py`。
**Test:** `tests/test_delivery_guidance.py`、既有 `test_verification_feedback_consistency.py` / `test_blocking_check_feedback.py` / `test_context_manager.py`。
**Interfaces:** 采用第 4.2/4.3 节签名；loop 是预算数值唯一来源；verification helper 只读 AgentRunState。

- [ ] RED：16 轮提示时点/数值、max_rounds=1/2/3、非法边界、取消不追加请求、不同 user_turn 重置；同轮多工具只计一轮，protocol correction 仍消耗原轮数。
- [ ] RED：真实 Runtime 修改根源码 → 定向 tests 成功后 pending 明确说明 → 读取文件仍 pending → 补检查后才可 finish。对比 root discover 路径无需先失败一次 finish；发现测试数量必须大于 0。
- [ ] RED：旧失败优先于覆盖提示、零测试、过期/伪造 evidence、普通脚本成功、检查后再改、多路径/长路径/敏感路径反馈；纯确认轮不新增验证义务。
- [ ] 最小实现追加式预算提示和展示 helper；不改 completion、check_coverage、TaskValidationTracker、ProgressGuard 或终止/资源管理。
- [ ] 用含真实 TokenUsage 的 fake Provider 验证第 1→2、13→14→15 轮前缀兼容，ContextManager 能继续使用 provider 锚；验证 memory off/structured 与 native/legacy 的实际 prepare→Provider 消息、配对、序号和任务归属；摘要输入仍将预算归为不可提炼的 protocol_feedback，跨 turn 不把它恢复为执行约束。禁止通过删除已计费消息或硬编码减 token 过测试。
- [ ] 执行受控端到端链路：必要编辑 → 业务测试 → 覆盖补齐 → 预算内 finish；以及最后一轮仅测试成功但没有 finish，仍保持 round_budget_exhausted。不得增加隐藏请求或把 incomplete 转成功。

### Task C：V3.1 公开验证指引及离线合同

**Create:** `evals/quality-v3.1/`、`tests/test_eval_quality_v3_1_guidance.py`。
**Modify:** 根 README 的当前套件说明；如需复用测试 helper，只做参数化并保留旧默认及旧断言。
**Interfaces:** 现有 load_suite/load_experiment/plan_trials/run_isolated_case；复用 `tests/quality_v3_support.py` 的 test-only 参考实现，禁止复制到模型 workspace。

- [ ] RED：新身份可加载、旧文件 hash 不变、全 30 题默认命令一致、业务配置和 verifier/public-test 字节相同、四份计划身份集合及数目正确。
- [ ] 新建套件并只修改第 4.4 节允许的文件；保存 before/after 差异列表。不要全局替换旧 benchmark 名或删除旧包。
- [ ] 全 30 题隔离执行新的公开命令并记录实际测试数量/exit；使用与当前 turn 契约匹配的正确参考实现，原始带 bug fixture 不要求通过。memory 多轮按公开阶段逐步验证，后续修正不能提前出现在首轮文档。
- [ ] 对 30 题运行 fake Provider + 真实 Runtime 的已授权修改/公开检查/finish 链路，确保命令真的经过政策、证据和 finish 评估，不用手工 RunResult(ok=True) 冒充完整交付；只读确认 turn 可直接 finish。
- [ ] 使用现有变体数据复验新套件的错误拒绝与合法接受；旧 125 基础/替代变体与后续 26 修复变体预期不变，保留旧套件测试。hidden 字节一致和端到端评分均需证据。
- [ ] 新旧四清单分别 dry-run，不构造真实 Provider；README 明确新套件“离线验收完成，待独立复审及授权真实复测”，不得预写提升分数。

### Task D：集成验证与交接

- [ ] 按下节运行必要专项、全部 Eval 测试、完整 Python 测试；保存实际 tests/failures/errors/skips/exit，不复制旧次数为本轮成绩。
- [ ] 核对代码/题库/历史证据差异白名单，`git diff --check`；同时检查新增未跟踪文件。不以 HEAD diff 替代 actual before/after。
- [ ] 生成 `docs/reviews/2026-10-27-quality-delivery-guidance-fixes-implementation.md`，列出每项合同的实现位置、测试、限制和未完成项；更新 project.md 与 reviewer handoff，保留旧正文和旧结论。
- [ ] 停在“实现完成，待独立复审”。不代替 reviewer 关闭问题，不自动运行真实模型。

## 6. Review Focus 与必须保留的负例

1. **时效/失败台账**（Task B）：旧测试失败未被同检查有效复验；另一成功检查或覆盖提示不能消除它。
2. **小预算与多轮**（Task B）：max=1、纠正到最后一轮、多 user_turn、cancel；提示不产生额外额度或继承上一 turn 的剩余额度。
3. **消息与 token 前缀**（Task B）：提示进入真实请求并正常计费，先前消息不重写；工具调用与结果仍成对，压缩后不恢复验证权限。
4. **错误/路径污染**（Task A/B）：恶意补丁、过长/敏感路径、伪造检查；固定建议不能回显正文或拼接未经验证的命令。
5. **评测可比性与零发现**（Task C）：新默认命令可能因包结构发现 0 tests；必须检查实际数量、隐藏逻辑不变、旧套件及 90 次历史不变。

## 7. 验证命令与通过标准

使用现有 `.venv\Scripts\python.exe -X utf8 -B`，日志各存新文件。unittest 的 discovery 会把 tests 加到导入路径，优先沿用此方式，避免项目现有 helper 导入失败。

```powershell
.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p "test_patch_feedback.py" -v
.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p "test_round_budget_feedback.py" -v
.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p "test_delivery_guidance.py" -v
.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p "test_verification_feedback_consistency.py" -v
.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p "test_blocking_check_feedback.py" -v
.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p "test_context_manager.py" -v
.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p "test_eval_quality_v3_1_guidance.py" -v
.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p "test_eval*.py" -v
.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -v
git diff --check
```

四清单 dry-run：对 `quality-v3` 与 `quality-v3.1` 各运行 `python -m tricoder eval --experiment evals/<suite>/experiment-quality-<baseline|repeat3|dev|holdout>.toml --dry-run --no-color`，分别应为 30/90/22/8，exit0，无密钥读取和模型调用。

通过标准：全部新增行为合同和既有相关负例通过；全量 0 failures/errors，skip 逐项注明环境原因；新增端到端用例真的执行公开测试且数量非零；本轮之外的源码/旧套件/旧证据 hash 不变。先前全量为 2060 项、2045 通过/15 skip，仅供历史参考。

对于受影响的取消、清理、收尾、普通命令、compileall 缓存、默认 strict 以及真实 search timeout 局部期限，必须保留并运行现有回归。不能因改动“主要是提示词”删掉这些边界测试。

## 8. 真实效果验证及归因

此方案的离线通过仅证明机制可用，不能证明模型会采纳建议，也不保证交付达到 90/90。后续由 reviewer 复审，用户另行授权真实实验。

建议未来分开看两个变量：先用修后 Agent 在不变 V3 上按 30×3 跑 Agent-only；再在同一修后 Agent 上对 V3.1 做全量，测指引变化。两批都涉及费用，本文不是运行授权。不能把不同 benchmark 结果强行送入 eval-compare 或只重跑三个失败样本当全量成绩。

保留本轮同口径指标：artifact/delivery pass@1/2/3、delivery pass^1/2/3、main/summary token、失败消耗摊销、单题 total/agent/verifier/harness、整批墙钟；另由脱敏审计统计 patch_count_mismatch、file_already_exists、finish recovery 和 round_budget_exhausted 次数，不必为此扩展生产结果 schema。报告观察变化及代价，不把有限三次样本称为显著改善。

## 9. 本文编写时的核查证据

`runtime/quality-delivery-guidance-plan-20261027/` 保存本次独立核查：821 份源码/测试/脚本/题库与真实复测版本 hash 相同；30 份题目 README 均首选定向 tests；命令分类/覆盖探针重现差异。现有 patch feedback 8 项、verification consistency 6 项、blocking feedback 5 项、context manager 8 项，共 **27 项通过，0 失败/错误**。这是旧行为基线，不能作为未来实现的 GREEN。

本轮仅新增方案、coding 提示词与核查证据，更新交接记录；没有修改生产代码/题库、调用模型或新增真实实验。本方案的轮次阈值（剩余 3 轮）和新版题库名称是 reviewer 的实施建议，尚无真实收益结论。

独立 builder check 指出草稿新增消息 kind 会被当前分组过滤且不在摘要排除规则内；现已改为复用 protocol_feedback，加入输入/摘要/协议计数回归。`message-kind-probe.json` 实测新 kind 被删除、复用 kind 被保留；只读复核确认两个方案缺口关闭。这是方案自审发现，不是新增生产缺陷或已完成的产品修复。
