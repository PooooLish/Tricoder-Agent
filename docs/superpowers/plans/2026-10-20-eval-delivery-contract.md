# Eval 交付诊断、对话契约与预算实验 Implementation Plan

> 实施状态（2026-10-20）：实现完成，待独立复审。勾选是coding记录，不代替独立验收；证据和限制见 `../../reviews/2026-10-20-delivery-fixes-implementation.md`。

> **For agentic workers:** 使用 superpowers:executing-plans。用户将在独立 coding session 实施；不派生子 Agent、不新建会话、不自动提交。先完成 `2026-10-20-agent-delivery-feedback.md` 的独立任务，再执行本包。

**Goal:** 让失败有可验证的原因，明确非交互前置消息的期望，并准备不污染旧基线的 8/16 轮实验。

**Architecture:** 增加白名单诊断事实，不改变评分真值；复制题库形成 quality-v2.2，只修改公开交互说明。预算比较使用独立冻结副本，保持原版本和比较器边界。

**Tech Stack:** Python 3.11+、现有 dataclass/TOML/unittest/Eval CLI；不新增依赖或外部服务。

**Spec:** `docs/reviews/2026-10-20-delivery-failure-investigation.md`，F3、前置确认和预算核查。

## Global Constraints

- 遵守根/项目 AGENTS；所有已有修改保留，实际 before 快照为基线，HEAD 不作为本次前态。
- 不改默认权限；评测原自动批准行为保持。不修改取消、清理失败、未知影响和验证 capability 门禁。
- 不改 `_combine_results` 的 all/AND、不把 needs_input 或 finish 工具成功算作 Agent 完成、不忽略失败的前置 turn。
- quality-v1、quality-v2（当前 benchmark=quality-v2.1）、历史冻结 A/B、旧结果全部保留。旧记录缺诊断显示 unknown，不回填推断值。
- 不读取 .env.local、真实密钥或用户数据库，不安装依赖、提交/推送；本实施包只跑离线验证、准备真实实验，不新增真实调用。
- 所有新运行证据保存在独立 `runtime/delivery-fixes-20261020/implementation-<唯一后缀>/` 子目录。

## Review Focus

1. completed/incomplete 两种 finish 都可能 tool.ok=true，不能混淆（Task 1）。
2. 预算在第 8 轮与第 8 轮主动澄清是不同终止原因（Task 1）。
3. 第一 turn 失败、最后 turn 成功，整体仍失败，旧结果未知值保留（Task 2）。
4. 新题面不得提前泄露第二 turn 决定或 verifier 内容（Task 3）。
5. 8/16 轮除了预算之外必须保持代码/夹具/题面/审批/模型/记忆一致，不能伪装严格比较器接受了它们（Task 4）。

## Task 1：保留可信的结束原因与 finish 声明

**Files:** `src/tricoder/engine/telemetry.py`、`engine/loop.py`、`engine/completion.py`（仅诊断）、`models.py:RunResult`、`execution_state.py`；必要的 `session/runtime.py` 终态接线。新增 `tests/test_delivery_stop_diagnostics.py`，回归 finish/cleanup/runtime 测试。

**Interfaces:**

- `execution_state.py` 增加固定 `TaskStopReason` enum：`completed`、`model_incomplete`、`round_budget_exhausted`、`needs_input`、`tool_stop`、`verification_blocked`、`cancelled`、`cleanup_failed`、`unknown_effects`、`progress_stopped`、`provider_failed`、`runtime_rejected`。
- RunResult 末尾追加 `stop_reason: TaskStopReason | None = None`，保持原位置参数及默认行为。None 表示未知/旧调用者；不要通过中文 summary 文本推断。此字段是诊断，不参与完成判定或审批。
- finish 参数审计只接受规范化 outcome（省略按 completed），非法值不原样保存；保留 summary_chars，仍不记录 summary 正文。
- 添加固定 `finish_assessment` 审计事件：`declared_outcome`、`accepted`、`validation_status`、`unresolved_check_count`、`pending_path_count`、`reason`（同一固定原因集合/unknown）。只读现有状态和 FinishAssessment；不得执行第二次完成判定、更改证据或恢复次数。

- [x] RED：相同 summary 的 completed/incomplete 在审计必须可区分；非法 outcome/任意字符串/合成秘密标记不得泄漏。tool.ok 不能被当作 accepted。
- [x] 在真实终止分支赋值 stop_reason：for 循环耗尽→round_budget_exhausted；needs_input 分支→needs_input；显式 incomplete→model_incomplete；已请求 completed 被验证拒绝→verification_blocked。其他已有分支依各自可信事实接线，无法证明时 None。
- [x] 原因优先级沿用既有最终结果：取消、清理失败、UNKNOWN 等安全终态不被 completed 覆盖；失败后 runtime 再追加提示不覆盖已知第一失败原因。只有原本成功、后来被 runtime 门禁否决，才使用 runtime_rejected。不要为传诊断更改资源释放顺序或门禁控制流。
- [x] RED/GREEN 验证同样 rounds=8 的两条路径分别得到 budget/needs_input；验证第 7 轮 incomplete、stale 阻塞、正常完成、provider 异常和 runtime 后置否决。
- [x] 审计写入失败仍按已有失败语义处理，不能为了诊断绕过 audit failure。取消/清理后禁止发起额外模型请求。

## Task 2：Eval 逐轮诊断安全往返

**Files:** `src/tricoder/evals/models.py:EvalTurnRecord`、`service.py:_ExecutionObservation/_EvalAgentObserver`、`report.py`；`tests/test_eval_diagnostics.py`、`test_eval_report.py`、`test_eval_scenarios.py`。

**Interfaces:** EvalTurnRecord 末尾追加可选字段：`finish_outcome: str | None = None`（completed/incomplete）、`stop_reason: str | None = None`（上面 enum 值）、`validation_status: str | None = None`（沿用 TaskValidationReport 状态）、`unresolved_check_count: int | None = None`、`verification_required: bool | None = None`。全部严格验证类型，bool 不得作为 int，计数非负。旧数据缺失显示 unknown，不能补 0。

- [x] 从真实 turn 的 RunResult 读取最后三项和 stop_reason；finish_outcome 只记录本 turn 成功解析执行的最后一次 finish 声明，通过 observer 获取，不从 Provider 文本或摘要猜测。每个新 turn 重置声明状态，防止跨轮污染。
- [x] 序列化/反序列化新增可选字段；旧 JSON 仍可读取；旧 passed 数值、指标分母、all/AND 不变。非法枚举及私有字符串在边界拒绝。
- [x] Markdown 单独诊断表保留 Condition/Case/Repetition/Turn；实际 finish 声明与最终 turn.ok 分列显示。None 显示 unknown；不新增“finish 工具成功率”等易混淆主指标。
- [x] 可在诊断摘要展示首个失败 turn 与最后 turn 的原始 ok，但完整通过仍要求全部 turn 成功。不要修改 hidden verifier 结果、覆盖范围或 cleanup 计算。
- [x] 真实 Runtime 三组：纯前置确认→后续修复均成功；前轮 needs_input→后轮成功整体失败；前轮成功→后轮验证失败整体失败。另测零修改纯确认不需要测试。
- [x] 使用旧 v1/A/B/v2.1 原生结果只读兼容验证；旧 JSON 不回写。验证 6/1/1/2 的历史 passed 数仍一致，多 trial 表格身份完整。

## Task 3：新版本公开交互契约

**Files:** 新建 `evals/quality-v2.2/`（来自实施前 quality-v2 的实际冻结内容）；新增 `tests/test_eval_quality_v22_suite.py`。若仅修改题面不需要新模型字段，禁止为此新增 user_turn 类型或通用会话框架。

**Interfaces:** suite id=`quality-v2-2`，benchmark_version=`quality-v2.2`，目录 quality-v2.2；保留 30 个 case ID、19 quality/11 contract、split、max_rounds=8、原 workspace/verifier、19 个 workspace_root 模式及 allowed/required。主 manifest id=`quality-v2-2-baseline`、suite="."，其余使用已有 DeepSeek/off-off/seed23 配置。

- [x] 复制前保存来源 hash，不覆盖旧题库。11 contract 的定义/工作区/脚本逐字一致；19 quality 工作区和 verifier 逐字一致。
- [x] 仅在 19 quality 的公开 user_turn 文本增加非交互说明：“当前为非交互执行；已有信息足够时自主完成。确实缺少会改变结果的必要信息时，应明确报告无法继续，不能编造回答。调用 ask_user 将无法获得即时回答。”不隐藏已有工具或假装提供交互宿主。
- [x] 3 道 memory 首轮明确“本轮只确认并记住这条约束/决定，不修改文件、不运行测试；确认后结束本轮，等待下一条任务”。mm-todo-once 的 a 已完成、mm-early-constraint 调用方颜色、mm-latest-correction 初始 red 原语义保留；不得添加未来 latest/空字符串的修正。
- [x] 后续实际编码 turn 保持既有业务需求与公开范围；允许读取前轮历史，不跳过前轮执行/评分。禁止把隐藏输入/断言/实现写进题面。
- [x] 新测试校验版本、19/11 数量、所有来源夹具 hash 和精确允许差异；确认直接 finish 前置消息可成功、无修改无验证，不能强制每轮工具探索。
- [x] 对新 manifest `tricoder eval --experiment ... --dry-run --no-color`，计划 19 quality。真实 30 题 contract+quality 不能冒充 30 道真实模型质量题。

## Task 4：准备独立 8/16 轮实验，不改产品默认

**Files:** 新增 `scripts/prepare_delivery_budget_experiment.py`、`tests/test_delivery_budget_experiment.py`；新题库 README 记录运行方式。全部生成文件只写用户指定的项目内新 runtime 目录，不覆盖已存在目录。

**Interfaces:** 脚本参数 `--suite <项目内源目录> --output <项目内全新目录>`；生成 `budget8/suite/`、`budget16/suite/`、各自 experiment.toml、source-sha256.json、allowed-delta.json。只复制明确的题库文件，拒绝链接/越界；不读取配置或调用 Provider。

- [x] RED：输出已存在/越界时拒绝；默认只生成不执行。固定来源应是同一个修复后 quality-v2.2 和同一源码快照。
- [x] 8 组保持19quality max_rounds=8，16组只将这19题 max_rounds 改16；11 contract 保持逐字相同。其余任务/夹具/隐藏测试/manifest 设置完全一致（必要输出路径及实验 id 除外），并生成机器可核对的精确差异。
- [x] manifest 均19题1次、相同seed23、DeepSeek deepseek-v4-flash、memory off/off、调度预算3600秒；dry-run 两组各19。默认产品 max_rounds 不变。
- [x] 复用核查的同一9轮合成序列，8组停止、16组第9轮成功；另测增加轮数不能消除真实旧失败/清理问题。
- [x] 不改现有严格比较器的允许变量，也不伪造相同预算指纹。它拒绝跨预算严格比较是预期。后续预算实验报告必须标明预算为设计变量，并附 allowed-delta 校验；与旧 benchmark 只能作历史描述性对照。
- [x] 不在本实施包自动运行38次真实调用。用户后续要求重跑时，执行冻结两组并分别报告产物/完整率、实际停止原因、token/耗时、单次样本限制；不得把本轮离线结果宣称为模型改善。

## 完成检查

- [x] 诊断新增键不改变旧指标，错误 enum/超长正文/旧数据缺失/多条件多重复均有测试；不存在 summary 文本推断终止原因的捷径。
- [x] 新版本不泄漏未来 turn、不改 hidden verifier，旧基线和历史结果 hash 不变；真实模型不新增调用。
- [x] 聚焦运行 test_eval_diagnostics.py、test_eval_report.py、test_eval_scenarios.py、test_eval_loader.py、test_eval_compare.py、新题库与预算脚本测试；运行 Agent 包相关测试，`git diff --check` 和未跟踪文件检查。
- [x] 记录每组实际测试数/跳过/退出码、源码前后索引、两组 dry-run；更新 project.md / reviewer-handoff 为“实现完成，待独立复审”。保留原 15/19、2/19 真实成绩，不能宣称修复后完整率已提高。
