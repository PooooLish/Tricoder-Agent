# Project: tricoder-cli

## 2026-10-07 工作区基线 F1/F2 补齐独立复审（通过）

- 原始独立探针重跑确认 F1/F2 关闭：激活时接受变化现在进入 Provider 首轮实际消息，且 pending 路径保留、只确认一次；kind 数组类型损坏恢复为 corrupted，Runtime 保留，不再漏出 TypeError。任务入口变化和普通非法 JSON 对照保持正确。
- 审阅新增真实 Runtime/Store/RecordingProvider 与 Shell 回归，覆盖恢复、切换、拒绝、存储失败和损坏原文/revision 保留。没有发现本轮范围内新的阻断问题。
- 新鲜相关组合 432 项、126.456 秒、OK（6 skipped），0 failures/errors；git diff --check 通过。本轮未重跑全量，不将实施方 1628 项结果冒称独立执行。
- 证据：`runtime/workspace-baseline/review-fixes/review2-result.md`、`review2-focused.log`、`review2-probes.log`。下一步可由用户进行恢复/切换/拒绝的本地人工验收；F1/F2 不再列为待修。真实模型/用户库及其他平台未验证。
- 本轮仅新增审查记录/交接，无生产代码或正式测试修改，无依赖安装、提交或推送。

## 2026-10-07 工作区基线持久化独立复审补齐（修复完成，待复审）

- 本轮严格限定为独立复审 F1/F2，保留当前未提交工作树与既有 `compileall -r`、验证义务、失败任务记忆和默认开启记忆修改；没有从 HEAD 覆盖文件。修复前原探针再次确认：激活接受变化后下一轮 Provider 收不到变化提醒，`entries[].kind=[]/{}` 会从恢复入口漏出 `TypeError`。
- F1：`_activate_current_workspace_baseline` 现在只在用户确认、复扫和基线持久化全部成功后发布一次性 `workspace_change_notice`。通知基于 `_observe_workspace_change` 更新后的当前 `ActiveSession`，不会用激活前对象覆盖 `pending`、`legacy_unknown`、UNKNOWN 或验证失效状态；拒绝和存储失败均不发布“已确认变化”。重启恢复与 Session 切换的首轮 Provider 请求都收到安全路径提醒且不重复确认，任务入口原有提醒行为保持。
- F2：持久基线解析在集合判断、比较和摘要计算前严格校验顶层/条目容器及字段类型；`null`、布尔、数字、数组、对象和非法字符串等可预期损坏统一为 `BaselineRecordError("corrupted")`，Store 继续映射为固定 `WorkspaceBaselineStoreError`。没有吞异常、返回无记录、自动重建或输出 payload；真实 Runtime 恢复和 Shell 切换均保留 Session 与原 payload/revision，文件任务阻断且 Provider 调用为 0。
- RED：正式专项初次 `Ran 13`，得到预期 2 failures + 4 errors；错误分别命中缺失通知和 `kind` 数组/对象的未捕获 `TypeError`。GREEN：类型/集成专项 `Ran 14`，OK；基线/Store/恢复/门禁组合 `Ran 39`，OK；工作区、Session、Shell、Console/TUI、验证义务和任务记忆组合 `Ran 548 tests in 136.468s`，OK（6 skipped）。
- 最终完整项目回归 `Ran 1628 tests in 304.383s`，OK（13 skipped）；13 项均为既有平台或权限条件，本轮新增测试无跳过。运行中保留了既有 asyncio/Textual/MCP 慢回调诊断，没有失败。证据见 `runtime/workspace-baseline/review-fixes/verification.md`。
- 未验证 Linux/macOS、Python 3.12、真实 Provider、真实用户数据库迁移和手工 Console/TUI。未读取 `.env.local`、真实凭据或用户会话库，未调用真实模型、安装依赖、提交或推送；`git_diff`、Plan/Replan、摘要和沙箱不在本轮范围。

## 2026-10-07 工作区基线持久化独立复审（2 项 P2 待补齐）

- 新鲜相关组合 424 项、129.504 秒、OK（6 skipped），git diff --check 通过；本轮未重跑完整 1620 项。此前 compileall -r 原始探针重跑通过，范围义务跨重启保留。
- F1：激活接受变化后只安装/持久化基线，没有向下一轮 Agent 设置 workspace_change_notice；第一条任务已无差异，通知永久漏发。真实 RecordingProvider 对照显示：激活时变化无提醒，任务入口变化有提醒。
- F2：持久化 JSON 的 entries[].kind 为数组/对象时，baseline_record.py 集合判断漏出 TypeError；恢复 Runtime 构造失败，而不是保持会话、标记 corrupted。普通非法 JSON 对照正常进入 corrupted。
- 证据与最小方向：`runtime/workspace-baseline/review-findings.md`；可执行 `review-probes.py`、`review-probes.log`、`review-focused.log`。下一步交 coding session 把两个场景转为正式失败测试并补齐后复审，保留现有正确行为。
- 本次仅追加诊断和审查记录，未修改生产代码/正式测试，未访问真实模型或用户库、安装依赖、提交或推送。

## 2026-10-07 工作区基线持久化与自动恢复（实现完成，待复审）

- 按 `docs/superpowers/plans/2026-10-07-persistent-workspace-baseline.md` 完成 T0—T5。新增不含正文的稳定基线投影、严格 JSON 校验、独立 SQLite 表、初始化标记与 revision CAS；跨重启摘要不复用含 inode 的运行期 `snapshot_id`。数据只含版本、工作区/范围绑定、根身份和路径/类型/大小/SHA-256/权限，拒绝不完整、损坏、超限、未知版本、绑定错误、缺失和孤立记录。
- Session 激活、显式创建、切换和恢复现在都会在短时工作区锁内扫描。无记录自动初建；一致时安装当前新鲜运行期快照；变化时先展示差异、确认后复扫并持久化。拒绝只取消当前操作，保持目标 Session 与旧 revision；锁忙、扫描失败或损坏记录不会调用 Provider，也不会静默覆盖。任务开始仍重新持锁复查，合法任务收尾和成功撤销才推进记录，存储失败保留旧基线并阻止虚假成功。
- 接受基线只认可执行起点：既有 `pending`/`legacy_unknown`、UNKNOWN 和检查事实不被清除，可信验证 evidence 不跨重启恢复。A/B Session 保持独立基线；同内容原子替换不误询问，根/范围/目录/权限/内容变化仍可见。跨重启只提供文件级差异，同进程继续沿用内存文本差异。
- Console/Shell/TUI 使用结构化 `workspace_baseline_state/message` 展示激活、拒绝、锁忙和损坏状态；工作区拒绝是正常取消，不冒充任务运行失败。首次完整回归发现旧 TUI 测试宿主缺少新增状态字段、spill 隐私测试未包含新表，均补兼容后重跑通过。
- 新鲜验证：计划八组聚焦回归合计 `Ran 408`，OK（6 skipped）；最终完整项目 `Ran 1620 tests in 324.794s`，OK（13 skipped）。13 项为既有平台、权限或可选能力条件跳过，本轮新增持久基线测试无跳过。最终编译、差异检查和完整命令记录见 `runtime/workspace-baseline/verification.md`。
- 兼容与限制：迁移只增表和 `workspace_baseline_initialized` 列，旧 Session 首次激活会自动建立当前起点并明确无法核对此前历史；第一版没有损坏记录修复命令，也不防本地数据库主动篡改，无法发现“修改后又恢复相同内容”的历史事件。未验证 Linux/macOS、Python 3.12、真实 Provider、真实用户数据库迁移或手工跨进程 Console/TUI；未读取 `.env.local`、真实凭据/会话库，未安装依赖、调用真实模型、提交或推送。非 Git 工作区的 `git_diff`、Plan/Replan 和摘要功能不在本轮范围。

## 2026-10-07 compileall 递归深度验证范围补齐（修复完成，待复审）

- 独立复审遗留的 P2 已按最小范围修复：`_check_scope_is_filtered` 现在把显式 `compileall -r` 识别为范围受限检查。命令仍真实执行并保留退出码，但成功结果会带 `scope_filtered` 诊断，不签发完整工作区验证能力。
- 修复前两条正式回归均按预期失败：历史恢复链路缺少 `scope_filtered`，本轮子目录写入则被错误交付为成功。修复后，`legacy_unknown + sub/broken.py` 在 `compileall -q -r 0 .` exit=0 后及 SQLite 重启后均保留；本轮子目录写入也继续要求有效验证。无 `-r` 的完整根检查正向行为保持不变。
- 新鲜验证：新增专项 `Ran 2`，OK；验证义务模块 `Ran 28`，OK；验证、Session、记忆、结束协议及 Console/TUI 聚焦组合 `Ran 429 tests in 91.065s`，OK（1 skipped）。复现探针显示受限命令 diagnostics=`scope_filtered` 且义务跨重启保留；探针 exit 0 只代表执行完成。
- 最终完整项目回归 `Ran 1596 tests in 229.746s`，OK（13 skipped）；13 项均为既有平台/权限条件跳过，本轮新增测试无跳过。证据见 `runtime/verification-obligation/review-fixes/review2-fix-verification.md`。未读取 `.env.local`、真实凭据或真实会话数据库，未调用真实模型、安装依赖、提交或推送。

## 2026-10-07 验证义务补齐独立复审（剩余 1 项 P2）

- 原 F1/F2/F3 原始探针已修复：cwd 范围、明确零测试、legacy_unknown 与已知路径并存均符合预期；过期通过负向对照仍有效，重启后的只读回顾正常交付。
- 仍有范围漏判：`tools/command.py:_check_scope_is_filtered` 遗漏已允许的 compileall `-r`。真实合成链路中，子目录源码语法错误且状态为 legacy_unknown + 子目录 pending 时，根目录 `compileall -q -r 0 .` exit=0 被当成完整范围检查，误清为 none，SQLite 重启后仍为 none。无深度限制的对照真实失败并正确保留状态。
- 新鲜聚焦回归 427 项、89.978 秒、OK（1 skipped）；git diff --check 通过，本轮未重跑全量。记录与探针位于 `runtime/verification-obligation/review-fixes/review2-findings.md`、`review2-depth-probe.py`、对应日志及 `review2-focused.log`。
- 下一步：把显式递归深度限制纳入 scope_filtered，增加真实恢复链路和当前写入门禁回归；保留无筛选有效检查的正向行为。当前复审暂不通过；不回退已经修好的三项。
- 本次仅新增诊断/审查记录并更新交接，无生产代码或正式测试修改；无真实用户数据/模型、依赖安装、提交或推送。

## 2026-10-07 验证义务复审补齐（修复完成，待复审）

- 按 `docs/superpowers/plans/2026-10-07-verification-obligation-review-fixes.md` 完成 R0—R4。R0 使用真实 `SessionRuntime`、临时 SQLite、fake Provider 和关闭/重启恢复入口，将 cwd 丢失、零测试误清义务、`legacy_unknown` 被覆盖三项探针转为正式 RED；失败均命中预期断言，不是夹具、导入或 Provider 队列错误。
- F1 在 Runtime 消费完整 `CommandCheckRecord`，保留 cwd、目标、authority 和快照时效；通过 `WorkspacePolicy` 将 `cwd + target` 解析到统一工作区坐标。`sub/.` 只覆盖 `sub`，路径比较保留分段边界、Windows 大小写规则，越界、链接逃逸、已消失或无法证明的目标不取得解除能力。只有与最终完整快照一致且仍由当前 scope 拥有的记录参与义务合并。
- F2 将“命令 exit=0”和“具有验证能力”分离。tests 明确报告 `zero_tests_reported` 时保留退出码、输出与诊断，但不签发通过 evidence、不清历史义务，也不能单独满足本轮写入门禁；之后的真实有效检查仍可满足门禁。带筛选/排除参数且范围不能证明完整的检查记录 `scope_filtered`，同样不取得解除能力；工作区实际变化仍发布负证据，不丢 UNKNOWN/失败事实。
- F3 独立维护历史未知来源和已知路径，最终编码为兼容三态：`legacy_unknown` 现在允许同时携带路径。局部合格检查只移除覆盖路径，完整无筛选根检查才能解除未知来源；工作区外部变化入口在失效前保留原义务，避免把原本 `none` 凭空升级为未知，也避免覆盖真正的未知。SessionStore 严格拒绝 `none+paths`、`pending+empty`、非法枚举/JSON/路径；Console/TUI/`/status` 展示组合状态。
- 首次完整回归 `Ran 1594`，出现 2 个失败，未记作通过：外部变化入口先强制失效后读取义务，把原本 none 临时变成 legacy_unknown。按根因修复后，两个失败与真正 legacy 组合对照均通过。最终完整回归 `Ran 1594 tests in 274.280s`，OK（13 skipped）。专项：验证义务 26、验证证据 80（1 skipped）、Session 116、任务验证 20、任务结果记忆 18、结束协议 20、TUI 35、Console UI 25，全部通过。
- 兼容与限制：数据库没有新增列，但写入 `legacy_unknown+paths` 后旧版程序可能拒绝读取；旧代码已经误清且没有来源的数据无法自动重建。未验证 Linux/macOS、Python 3.12、真实 Provider、真实用户数据库迁移或手工 Console/TUI。未读取 `.env.local`、真实凭据或用户会话库，未安装依赖、调用真实模型、提交或推送。证据位于 `runtime/verification-obligation/review-fixes/`。

## 2026-10-07 历史验证义务范围独立复审（3 项 P2 待补齐）

- 审查基线 HEAD `7c26bd7`，开始时工作树干净。原始“恢复会话后只读回顾误判失败”回归已通过；新鲜聚焦 333 项、54.041 秒、OK（1 skipped），本轮未重跑全量。
- 额外探针复现三项义务误清除：F1 检查目标忽略 cwd，子目录 compileall 点号可清除根目录 app.py；F2 unittest 明确零测试仍清除语法损坏 app.py 的义务；F3 legacy_unknown 被新 pending 覆盖，之后局部通过使历史未知也消失。三者均在临时 SQLite 重启后确认持久化。
- 证据与修复方向：`runtime/verification-obligation/review-findings.md`；可执行探针 `review-repro.py`、输出 `review-repro.log`，回归日志 `review-focused.log`。旧通过记录过期对照仍正确保留 pending，不列为缺陷。
- 下一步：coding session 将三项转为正式失败测试并最小补齐，之后复审；不回退当前回顾正常交付逻辑，不放宽 UNKNOWN 等硬门禁。审查仅新增诊断/记录并更新交接，没有修改生产代码或正式测试，未访问真实数据或模型服务、安装依赖、提交或推送。

## 2026-10-07 历史验证义务范围修复（修复完成，待复审）

- 按 `docs/superpowers/plans/2026-10-07-verification-obligation-scope-fix.md` 完成 R0—R4。R0 使用真实 `SessionRuntime`、临时 SQLite、fake Provider 和关闭/重启后的实际 `_build_active` 恢复路径，先稳定复现 3 个 RED：历史失败、历史通过及真实修改待验证都会在恢复后把只读回顾误判为本轮未完成。
- 根因是历史 `verification` 展示值、累计 `modified_files`、可用 evidence 和本轮 `verification_required` 共用同一状态。恢复入口把 passed/failed 降为待验证，Agent 初始化和 Runtime 收尾又据此建立本轮门禁；历史义务没有独立、可持久化的宿主来源。
- 新增 `verification_obligation=none|pending|legacy_unknown` 与规范相对路径列表。新会话显式为 none；旧表幂等补列，有已知修改路径迁为 pending，来源不足的旧检查/UNKNOWN 迁为 legacy_unknown，干净未运行记录迁为 none。枚举、JSON、唯一相对路径与状态/路径一致性严格校验，相关字段在既有事务中一并写入；不恢复 evidence、审批或通过能力。
- Agent 与 Runtime 现在只用本轮真实效果、当前 evidence 有效性及既有安全状态计算本轮门禁。文件、目录、命令副作用和净零写入仍建立义务；只读/no-op 不建立。历史 pending/legacy_unknown 不阻止回顾且不会被回顾清除；只有当前 Session authority、稳定最终快照和检查目标确实覆盖对应路径时才解除义务。UNKNOWN、取消、清理/审计失败、失效 evidence 和不完整扫描仍 fail-closed。
- Console/TUI/`/status` 分别展示本轮交付、本轮检查事实和历史修改验证。结构化记忆保持低信任，不能建立或解除宿主义务；F1/F2/F3 的失败终止、覆盖水位、刷新/保存与旧预览边界继续通过。
- 新鲜验证：义务专项 `Ran 12`，OK；计划相关聚焦组合 `Ran 341`，OK（2 skipped）；全部记忆 `Ran 87`，OK；Session store/Agent 邻接 `Ran 130`，OK。提交前最终完整项目 `Ran 1578 tests in 347.364s`，OK（13 skipped）；13 项均为既有平台/权限条件跳过。本轮证据见 `runtime/verification-obligation/verification.md`。
- 未验证 Linux/macOS、Python 3.12、真实 Provider、真实用户数据库迁移和手工 Console/TUI；测试仅使用合成工作区、临时数据库和 fake Provider。未读取 `.env.local`、真实凭据或真实会话库，未安装依赖、提交或推送。范围未扩展到摘要输入超限、Plan/Replan 或沙箱。

## 2026-10-07 连续任务迟到失败记忆补齐（修复完成，待复审）

- 本轮仅处理第二次复审的 F3/P2，保留工作树中已经完成的 F1/F2、失败任务记忆、`finish.outcome` 和记忆默认开启等修改。修复前探针稳定复现：首任务被 Runtime 最终取消后，候选停在消息 3、结束水位为 4；不刷新直接完成下一任务后水位到 7，候选仍为 3，自动更新、显式 refresh 与保存形成相互阻塞。
- 根因是 `ContextManager.extend_save_candidate_with_termination` 只检查最后任务，而 `MemoryCoordinator.prepare_review_candidate` 又在共享候选构建流程前用旧边界做严格审计。下一任务出现后，旧覆盖点落在前一任务内部，合法的宿主迟到终止事实无法补齐，后续完整任务也无法进入原有摘要流程。
- 现在先定位旧覆盖边界所在任务；仅当旧边界确实存在、边界前缀及完整任务均闭合、工具调用/结果配对完整，且该任务未覆盖尾部全部是同一任务的宿主可信终止事实时，才确定性合并到该任务边界。后续任务继续走严格 `plan_save_candidate`、摘要、候选校验与取消检查；普通消息、来源异常和未配对 ToolCall 仍拒绝。自动候选和 `/memory refresh` 共用该流程，完整构建成功前不发布半成品。
- 正式回归覆盖迟到取消、最终扫描否决与下一任务工作区确认、已有多任务坏状态刷新、旧预览失效、确认保存和重启恢复，以及普通尾部、未配对工具结果、摘要失败、校验失败、取消和重复刷新幂等。修复后不立即刷新与立即刷新的两组探针均达到最新水位 7，refresh/preview 均正常；第一次失败待办保留，第二次任务来源被覆盖，确定性补齐不增加摘要调用。
- 新鲜验证：新增专项 `Ran 4`，OK；记忆/SessionRuntime/验证聚焦 `Ran 200`，OK（1 skipped）；Agent/结束协议/压缩/CLI 邻接 `Ran 216`，OK；最终工作树完整项目 `Ran 1564 tests in 233.865s`，OK（13 skipped）。最终 `compileall`、`git diff --check` 和探针复验见 `runtime/task-outcome-memory/f3-verification.md`。
- 兼容与限制：未修改数据库 schema、Provider、保存确认或记忆默认配置；没有自动保存、重跑工具或直接推进覆盖水位。尚未验证 Linux/macOS、Python 3.12、真实 Provider、真实用户数据库和手工 Console/TUI。未读取 `.env.local`/真实凭据/真实会话库，未安装依赖、提交或推送。

## 2026-10-07 F1/F2 第二次复审（剩余一项 P2）

- 原 F1 检查顺序误建验证义务已修复；原 F2 的最终失败标记、旧候选/旧预览失效和立即 refresh/save 路径通过。新鲜聚焦 217 tests、51.089s、OK（1 skipped），git diff --check 通过；本轮未重跑全量。
- 新发现 F3/P2：Runtime 晚到取消后候选覆盖 3、结束水位 4；不立即 refresh 而直接执行下一任务，水位到 7，候选仍为 3。随后 refresh 报候选无效，保存又要求先 refresh，无法正常补齐。对照组先 refresh 再执行下一任务则正常覆盖到 7。
- 根因是 `ContextManager.extend_save_candidate_with_termination` 只允许末尾任务补齐；下一任务出现后旧候选边界落在前一个完整任务内部，自动候选审计与常规刷新都拒绝。下一动作：最小补齐旧覆盖边界所在任务的可信终止尾部，再按原严格规则处理后续任务；同时覆盖自动候选入口，保留工具配对、来源与保存审批限制。
- 证据和实施建议：`runtime/task-outcome-memory/review2-findings.md`；新增离线探针 `review2-followup-repro.py` 及对应日志；原问题复验日志 `review2-repro.log`、聚焦日志 `review2-focused.log`。暂不整体验收，先将连续任务复现转为正式测试再修复。
- 仅记录审查与诊断，未修改生产代码/正式测试；未访问真实数据库或凭据，未调用真实模型、安装依赖、提交或推送。

## 2026-10-07 检查顺序与 Runtime 最终失败记忆补齐（修复完成，待复审）

- 本轮范围严格限定为复审 F1/F2，保留当前工作树中的记忆默认开启、`finish.outcome`、失败任务记忆及其他既有修改；未从 HEAD 覆盖文件。
- F1 根因是 Runtime 把“当前通过证据是否可用于整体通过”与“该证据自身的 authority/快照是否有效”合并判断。另一项检查仍失败时，有效 evidence 被误判为过期并凭空建立 `verification_required`。现在先独立核验 evidence 的 Session 所有权与当前快照，再仅由真实修改、继承义务、UNKNOWN、取消/清理或失效 evidence 决定修改验证门禁。无修改审查的失败→通过、通过→失败均可交付，真实失败记录与 `TaskValidationReport` 保留；真实修改后仍有失败时继续拒绝成功，后续任务不会继承虚构义务。
- F2 根因是 Agent 已闭合历史并生成候选后，Runtime 的取消提交、清理门禁或最终工作区扫描仍能把结果改为失败，但旧历史、候选覆盖和保存预览没有同步。现在 Runtime 最终否决后只追加一次宿主签发的未完成事实，重新核对连续闭合边界并更新安全摘要；不伪造 `finish`、tool result、测试通过或业务完成，也不重复发送终态事件。旧候选对象保留但因覆盖不足不能保存，旧预览因消息序号变化失效。
- `/memory refresh` 对“同一末尾任务、旧覆盖点之后仅新增可信终止事实”的精确形态做确定性本地合并，不调用摘要模型或业务工具。跨任务、普通消息、真正缺失 tool result 或其他不完整历史仍走原有严格拒绝；刷新后确认保存并重启只恢复低信任未完成待办，不恢复原始消息、权限、审批或验证能力。
- 正式回归覆盖两种检查顺序、失败记录保留、真实修改门禁、下一任务隔离、最终扫描否决、取消/清理、单次终止标记、旧候选/旧预览失效、refresh 不重跑工具、确认保存与重启恢复，以及未配对工具调用拒绝。诊断脚本修复后显示三种无修改审查均 `ok=true`；最终扫描否决为 `ok=false`、终止标记 1、旧预览拒绝、refresh 额外摘要调用 0、刷新后候选含 pending 失败事实。
- 新鲜验证：F1/F2 专项 `Ran 94`，OK（1 skipped）；全部记忆测试 `Ran 87`，OK；Agent 记忆边界/Context/Session 组合 `Ran 102`，OK；最终完整项目 `Ran 1560 tests in 432.002s`，OK（13 skipped）。最终 `compileall` 和 `git diff --check` 结果见 `runtime/task-outcome-memory/verification.md`。
- 兼容与限制：未修改 SQLite schema、Provider 协议或记忆默认开关；旧候选需显式 refresh 后才能保存。宿主终止事实依赖进程内原始历史，重启前未确认保存的原始消息仍按既有设计不会持久化。未验证真实 Provider、真实用户数据库、Linux/macOS、Python 3.12 或手工 Console/TUI；未读取真实凭据/会话库、未安装依赖、提交或推送。

## 2026-10-07 检查结果与失败记忆复审（暂不通过）

- 当前工作树复审发现两项 P2：F1 `session/runtime.py:1498-1516` 把“先失败 A 再通过 B”的有效通过证据误判成无效能力，重新建立修改验证义务；同样检查交换顺序会改变审查任务结果。F2 Runtime 在 Agent 返回后将任务改判失败时，没有同步失败终止事实和候选覆盖；旧候选仍可保存，refresh 因覆盖相等而不更新。
- 合成真实工具探针已复现：失败→finish 为 ok=true，失败→通过→finish 为 ok=false，反序又为 true，三者均未修改文件；独立的 Runtime 最终扫描前外部修改探针得到 ok=false，但终止事实=0、候选待办=0、refresh 新调用=0，保存预览仍被允许。
- 新鲜聚焦回归 212 项、66.244 秒、OK（1 skipped）；没有重跑全量。已有通过测试缺少这些组合，不能据此判为验收通过。
- 审查记录 `runtime/task-outcome-memory/review-findings.md`；复现 `runtime/task-outcome-memory/review-repro.py`，运行日志 `review-repro.log`，聚焦日志 `review-focused.log`。复现脚本 exit 0 仅表示诊断运行完成。
- 下一动作：coding session 将 F1/F2 转为正式失败测试后最小修复并复审；保留已有正确行为和默认记忆设置。本轮只新增审查记录/诊断，未修改生产代码或正式测试，未访问真实会话库或模型服务。

## 2026-10-07 检查结果与失败记忆最小修复（R0—R4 已实施）

- 执行计划：`docs/superpowers/plans/2026-10-07-task-outcome-memory-minimal-fix.md`；证据：`runtime/task-outcome-memory/verification.md`。基线为 HEAD `cb617e4` 加现有“记忆默认开启”未提交修改；本轮保留这些修改，没有从 HEAD 覆盖文件。
- R0 用临时真实 unittest 和 fake Provider 固定三项 RED：无修改审查的 exit=1 被误写成“文件修改后的验证失败”；失败任务不推进记忆覆盖且 refresh 不调用摘要器；revision=0/coverage=0 的初始空记忆可以预览保存。另补 native/legacy `finish.outcome` 缺失契约。
- R1 为 `finish` 增加可选 `outcome=completed|incomplete`，省略兼容为 completed；它只表达交付声明。稳定失败检查继续保留真实 returncode、TaskValidationReport 和失败快照，但不再自动建立修改验证义务；真实修改、继承义务、过期/跨 Session 通过证据、最终扫描不完整、取消、清理和 UNKNOWN 仍 fail-closed。观察性失败显示“检查发现失败”，Console/TUI 继续独立显示交付、任务验证和“需求覆盖未自动确认”。
- R2 在 TaskFinalizer 集中追加单次可信未完成事实，并由 ContextManager 检查从旧水位到当前任务的连续完整历史后推进覆盖。兼容字段 `latest_completed_task_seq` 保留名称，但语义改为“最新可纳入记忆的已结束任务”，不是成功凭据。正常 finish（含 incomplete/宿主否决）复用候选整理；异常停止不增加摘要请求，之后 `/memory refresh` 可消费失败来源。真正缺失 tool result 的历史仍拒绝推进；摘要模型返回空候选也会由宿主确定性保留 pending 失败事实。
- R3 拒绝 revision=0、coverage=0 且无语义条目的初始空候选；revision 已推进的显式编辑/清理结果仍可保存。临时 SQLite 完成“异常失败→refresh→确认保存→重启”，只恢复低信任未完成待办，不恢复原始工具输出、验证证据、审批或文件状态。legacy Eval 明确保持 memory-off 旧基准；版本化 memory-on 实验仍走生产配置路径。
- 新鲜验证：核心新增 `Ran 12`，OK；验证证据 `Ran 77`，OK（1 skipped）；Agent 同步/异步 `Ran 101`，OK；计划指定四组分别为 12/6/20/20 项全绿。最终完整项目 `Ran 1555 tests in 228.689s`，OK（13 skipped）；13 项为既有平台/权限条件跳过，本轮新增测试无跳过。compileall 与 `git diff --check` 的最终结果记录在证据文件。
- 兼容与限制：没有数据库迁移，旧字段名继续存在；旧持久化状态没有足够来源类型时仍保守要求重新验证。模型可能错误选择 completed，本轮没有新增语义验收器；真实 Provider、真实用户数据库、Linux/macOS、Python 3.12 和手工 TUI 未验证。未读取 `.env.local`/真实会话库、未调用真实 Provider、未安装依赖、未提交或推送。

## 2026-10-07 会话记忆与结构化压缩默认开启

- 按用户要求将 `MemoryConfig` 默认值改为 `compaction="structured"`、`persistence="reviewed_summary"`；配置解析复用同一默认值，环境变量和项目配置的显式设置仍优先。完全关闭时需同时将两项设置为 `off`。
- 补齐一次性 CLI `run` 向 Agent 传递 `config.memory`，避免默认值改变后显式关闭配置被忽略。交互式 SessionRuntime 原有配置传递保持不变。
- 保留现有隐私与恢复边界：自动生成待审候选，`/memory save` 预览并确认后才持久化；不自动保存完整对话，不改变原有摘要校验、失败保留历史或审批机制。README 已同步说明默认值、额外摘要调用和重启恢复条件。
- 回归测试新增默认配置、显式关闭与环境变量优先级断言；默认 Agent 候选测试确认保留近期历史且未自动持久化。原先只测试工具事件、旧预算路径或控制步骤的离线用例显式关闭记忆，保持原断言，避免消耗未提供的模拟摘要响应。
- 验证：配置测试 42 项通过；CLI 配置传递修复后 35 项通过。最终完整离线回归 `Ran 1543 tests in 368.150s`，`OK (skipped=13)`，退出码 0；记录在 `runtime/memory-defaults-final.log`。`git diff --check` 通过，已自查生产改动和测试差异；13 项为既有平台/权限条件跳过。
- 本轮已完成，下一动作是用户重启 TriCoder 使用新默认值；原先显式关闭的工作区仍需自行调整配置。未安装依赖、读取真实密钥/会话库、调用真实 Provider、提交或推送；未验证 Linux/macOS、Python 3.12 或真实模型摘要效果。

## 2026-10-07 第三批 C1/D 复审补齐（已实施，等待复审）

- 范围严格限定为 `docs/superpowers/plans/2026-10-07-convergence-review-fixes.md` 的 F1/F2/F3。基线 HEAD 为 `43f721a`，保留开始时所有未提交和未跟踪修改；未从 HEAD 覆盖文件，未安装依赖、读取 `.env.local`/真实会话库、调用真实 Provider、提交或推送。
- R0 先运行 `runtime/task-quality-round1/review-c1-repro.py`：F1 的 `call-2` 有两条结果且任务块未闭合；F2 四次相同 spill 读取后仍消费第 5 个响应并成功；F3 A→B→A 后首次回读在第 6 次请求误停。三个场景均转为正式测试并分别出现预期 RED，不是 fixture 或导入失败。
- F1 根因是失败批次已补 skipped 后，进展扫描取消分支再次无条件回填。现在该分支复用 `remaining_filled`：已有结果不重复发布，未填批次只补一次；skipped 审计失败仍优先安全停止，`NativeCancellationError.cleanup_failed`、事件/审计单次性、历史闭合和后续保存候选均有回归。
- F2 为 `ToolResult` 追加默认 `None` 的 `progress_output_digest`。`ToolRegistry` 在截断/spill 前对完整正文生成宿主摘要并覆盖扩展自报值；Runner 只比较内容身份及错误/返回码，不比较随机引用、展示包装或原始 spill 元数据。文件正文中的时间字符串保持原义，宿主命令检查仍可规范化耗时噪声；`spill_sha256` 继续用于原有完整性审计。
- F3 增加独立读取阶段通知。只有成功、非中断且由内置变更账本/宿主观察确认为 `CONFIRMED` 的实际文件或目录变化才调用 `note_workspace_change()`；A→B→A 的两次真实写入均推进阶段。no-op `create_directory(exist_ok=true)`、UNKNOWN、取消和扩展自报路径不能重启读取；失败计数、A/B 振荡证据、32 条窗口和新任务隔离保持不变。
- 修复后诊断脚本实际输出：F1 每个 call ID 恰好一条结果且 `task_block_closed=true`；F2 在第 4 次读取停止且只消费 4 次 Provider；F3 消费到第 7 次 finish，按真实“修改后未验证”事实结束，不再因重复读取提前停止。exit 0 仍只表示脚本运行完成，正式测试才是验收主证据。
- 聚焦回归覆盖 ProgressGuard、真实工具批次、结束协议、批次失败、spill、扩展结果、审计、记忆压缩/保存和 SessionRuntime：`Ran 355 tests in 25.667s`，OK。最终完整项目：`Ran 1541 tests in 328.642s`，OK（13 skipped）。compileall、CLI `--help`、19 项 Agent/module 导入边界与 `git diff --check` 均 exit 0。
- 证据记录：`runtime/task-quality-round1/convergence-review-fixes.md`。现停在待复审交付点，不自行写成“复审通过”。未验证 Linux/macOS、Python 3.12、真实 Provider 与手工 Console/TUI；13 项跳过为既有平台/权限条件，本轮新增测试没有跳过。

## 2026-10-07 第三批复审补齐执行文档交接（待实施）

- 补齐计划：`docs/superpowers/plans/2026-10-07-convergence-review-fixes.md`，范围仅 F1/F2/F3，按 R0 复现、R1 单次回填、R2 稳定结果摘要、R3 读取阶段、R4 整体验收推进。
- 核心约束：F1 保留审计/取消与历史配对；F2 不让暂存引用进入进展判定、不削弱原始内容完整性检查；F3 仅重启读取区间，保留失败和振荡记录，no-op 不算进展。
- 第 10 节提供可直接交给 coding session 的提示词；诊断脚本仍为 `runtime/task-quality-round1/review-c1-repro.py`，必须转为正式失败断言再修复。
- 本轮只写文档和交接，未修改功能代码、未重新运行功能回归。下一动作：coding session 按补齐计划实施，交付后再复审，不把计划完成等同于缺陷关闭。

## 2026-10-07 第三批 C1/D 复审（未通过，3 项 P2 待补齐）

- 本轮审查最新工作树，保留三批既有改动；未修改生产源码或正式测试。使用 verification-before-completion 和 systematic-debugging 流程重新验证，不将此前交接中的全量结果视为本轮通过证据。
- 新鲜聚焦回归：`test_progress_guard.py`、`test_agent_convergence.py`、`test_agent_termination.py`、`test_memory_compaction.py`、`test_memory_save_coverage.py`、`test_session_runtime.py`、`test_audit.py`、`test_clarification.py` 合并运行，`Ran 144 tests in 15.360s`，OK。本轮未重跑全量；下列新增探针均稳定复现缺陷。
- **F1 / P2：进展扫描取消会重复回填 skipped。** `engine/tool_batch.py:284-296` 未检查 `remaining_filled`。同批第一个 read_file 参数错误时，后续 call-2 已被回填一次；随后进展扫描抛 CancellationError，取消分支又回填一次。真实 Agent 结果为 call-1 一条、call-2 两条 tool result，ContextManager 判定该任务块未闭合，影响后续压缩/保存。修复应保持回填单次、审计优先级与统一收尾，补可恢复失败＋多调用批次＋扫描取消的组合回归。
- **F2 / P2：spill 包装使相同结果绕过重复检测。** `engine/tool_batch.py:519-538` 对展示用 result.output 计算指纹，其中包含每次不同的暂存 reference；原始内容相同仍产生不同指纹。真实 ToolRegistry＋SpillStore＋Agent 连续读取同一大文件四次没有停止，继续发起第五次请求并 finish 成功。应从宿主原始语义结果生成稳定摘要，排除暂存引用等展示元数据；保留内容变化的可区分性，并防止原始 spill_sha256 抵消耗时归一化。
- **F3 / P2：读取预算未在真实写入后开启新阶段。** `engine/progress.py:175-186` 对全部窗口中相同 digest 的读取累计，只有 user answer epoch，没有变更阶段；成功写入也不进入 `_observe_progress`。真实 Agent 读取 A 三次、编辑 A→B→A 后首次回读，立即按累计四次停止。这违反计划中“实际内容变化开启新的只读计数区间”；应在确认的真实内容变化后更新读取阶段，同时保留重复失败和振荡证据，不让净零/no-op 或任意工具成功随意清空预算。
- 可复现脚本：`runtime/task-quality-round1/review-c1-repro.py`；从项目根执行 `.venv/Scripts/python.exe -B runtime/task-quality-round1/review-c1-repro.py`。只使用合成临时工作区和 fake Provider；F1 输出 `task_block_closed=false`，F2 输出 `ok=true, provider_calls=5`，F3 输出重复读取停止。脚本 exit 0 仅表示诊断运行完成，不代表验收通过。
- 下一动作：由 coding session 先将 F1/F2/F3 转成正式失败测试，再最小修复并复审；本轮三项关闭前不宣称第三批验收通过。未读取真实凭据或会话库，未调用真实 Provider，未安装依赖、提交或推送。

## 2026-10-07 失败收敛第三批 C1/D（已实施，等待审查）

- 先复核第二批 B1/B2：回答、超时、取消、迟到答案、无交互宿主、同批 skipped、回答不审批、等待期间外部修改、锁持有、历史闭合和记忆兼容新鲜组合 `Ran 236`，OK。最终 B/C/D 聚焦再跑 `Ran 200`，OK，没有绕过第一、第二批边界继续实施。
- 新增单任务 `engine.progress.ProgressGuard`，最多保留最近 32 条宿主指纹。相同状态的同一失败第 2 次提醒、第 3 次停止；相同无变化读取第 2 次提醒、第 4 次停止；同一失败检查出现完整内容状态 A→B→A→B→A 时停止。普通读取、无关成功和回答不删除失败记录；回答只重启读取区间，新任务重新创建 guard。
- 工具批次只在 effects 发布、真实结果配对和工具审计成功后观察。参数使用实际命令记录或工作区规范路径；结果对常见耗时文本及其规范化长度做摘要。只有 scope 签发的 information 命令可作为读取；不同读工具交替、路径别名和版本查询不能重置预算。不完整快照不证明状态相同。
- 新停止保留取消、审计失败、UNKNOWN、清理失败、原生无工具预算和 max_rounds 的既有优先级。同批剩余工具只补 skipped；停止走统一 finalizer，保留修改、真实失败、验证、usage 和账本，不自动撤销或伪造 finish/测试通过。固定终止事实支持后续压缩与保存，三类失败形成 pending 待办且不推进成功水位；真正未配对 ToolCall 仍拒绝。
- D 联调覆盖 ask_user→回答→重新请求→重新审批→创建→相关检查→finish，等待取消/外部修改、重复失败/读取/振荡、锁释放、新任务计数隔离和停止后记忆。自审另复现并修复：进展快照取消会越过统一收尾、原始输出长度使耗时字段仍旁路摘要、information 查询未计入重复读取。
- 新鲜验证（Windows / Python 3.11.6）：C1/快照/审计 `Ran 43`，OK（1 skipped）；最终 Agent/Runtime/验证/记忆/安全邻接 `Ran 414 tests in 92.488s`，OK（2 skipped）；B/C/D 聚焦 `Ran 200`，OK；完整项目 `Ran 1527 tests in 222.983s`，OK（13 skipped）。compileall 与 `git diff --check` 退出 0。证据见 `runtime/task-quality-round1/third-batch-verification.md`。
- 未调用真实 Provider，未读取 `.env.local`、真实密钥或用户会话数据库，未安装依赖、提交或推送。剩余限制：这是 32 条窗口的启发式保护；内容摘要为识别原子同内容写回而忽略身份/权限，严格验证仍使用原 digest；耗时规范化不能覆盖所有非确定性输出。Linux/macOS、Python 3.12、真实 Provider、手工 Console/TUI 尚未验证。

## 2026-10-06 需求澄清第二批 B1/B2（已实施，等待审查）

- 前置复核没有发现阻塞项：第一批普通脚本副作用观察、扫描 fail-closed、任务验证 authority/时效、无关检查不能清除失败，以及结束协议 P2 的失败历史/结构化记忆兼容组合共 `Ran 175`，OK（2 skipped）。没有绕过这些检查进入 B 阶段。
- 新增宿主无关的 `ClarificationRequest/ClarificationResult` 和内置只读 `ask_user`。宿主签发 request ID；问题、2—4 个可选建议和自由文本回答均有长度上限。回答只形成真实 tool result，不携带 permission、approval 或 verification authority；同批剩余调用只补 `skipped`，下一轮 Provider 必须基于回答重新决策。单任务最多 2 次有效提问，超限保守停止。
- 等待是进程内一次性状态，默认 300 秒，复用调用方取消令牌并保持 Session、任务和工作区锁。等待前后使用现有工作区验证范围重新扫描；扫描失败或外部变化时不采用答案，后续任务继续经过既有完整工作区门禁。取消、超时、无宿主、界面关闭和提问上限各有固定未完成终止事实；真实 ToolCall 先配对，失败不推进成功任务水位，记忆候选保留对应 pending 事实。
- Console 仅在真实终端使用可取消轮询读取，非交互和不具备取消契约的自定义阻塞输入明确 unavailable，不创建遗留 `input()` 线程。TUI 使用独立模态框、倒计时、Enter/Esc 和首次终态生效的线程安全等待；取消/退出关闭等待，迟到答案无效。两参数旧 TUI runtime factory 保持兼容但不提供澄清宿主。
- TDD 先复现并修复了回答后同批写入抢跑、取消/迟到答案、外部编辑、锁持有、历史/记忆阻塞、非交互输入及旧 TUI 退出宿主兼容问题；自审又补充同步 `ToolRegistry.execute` 必须沿用调用方取消令牌的回归。聚焦组合 `Ran 235`，OK；澄清专项最后代码 `Ran 18`，OK；工具/导入边界 `Ran 146`，OK。
- 首次全量 `Ran 1504`，出现 3 个失败，未记作通过：新增可选 Schema 未进入 Provider 契约期望；工具固定前缀增加 401 字符使 Eval fixture 多触发一次 compaction；命令闭环在全量压力下返回一次 UNKNOWN。前两项按根因修复后目标测试通过；命令闭环独立连续 8 次通过且第二次全量未复现。最终完整回归 `Ran 1504 tests in 226.888s`，OK（13 skipped）；compileall、19 项导入边界及 `git diff --check` 均通过。
- 本批没有实施 C1/D，不支持跨进程或跨重启恢复等待，也没有把有限快照观察表述为 OS 沙箱。未调用真实 Provider、未读取 `.env.local`/真实会话库、未安装依赖、提交或推送；手工 Console/TUI、Linux/macOS 和 Python 3.12 尚未验证。首次全量的命令 UNKNOWN 未能稳定复现，虽然后续 8 次专项和一次全量均通过，仍作为低概率 Windows/负载风险保留记录。

## 2026-10-06 任务验证第一批 S0/A1/A2（已实施，等待审查）

- 执行文档：`docs/superpowers/plans/2026-10-06-task-verification-clarification-convergence.md`；证据位于 `runtime/task-quality-round1/verification.md`。实施基线 HEAD 为 `43f721a`，保留了开始时未提交的计划与本文件交接内容，没有从 HEAD 覆盖工作树。
- S0 新鲜复核结束协议 P2：零工具和完整工具回合后的第三次 native 无工具响应均闭合失败历史；真实未配对 ToolCall 继续被拒绝；失败待办不会被空摘要删除，失败不推进成功水位。S0 指定组合 `Ran 109`，OK。
- A1 将“普通允许命令的副作用观察”与“认可验证命令的证据签发”拆开。非信息命令在审批后执行前、正常清理后捕获受覆盖快照；稳定普通脚本返回本次 effects NONE，但 `verification_passed`/旧证据保持空。前扫描失败或不完整不启动普通命令，后扫描失败、不完整或变化继续 UNKNOWN；原有 UNKNOWN、取消、超时、输出超限和清理失败不被清除。稳定非零退出保持 `execution_failed / replan`，没有放宽命令策略或审批。
- A2 新增 `core.validation.CommandCheckRecord/TaskValidationReport` 与 `engine.validation.TaskValidationTracker`。记录由本地 scope 签发并绑定历史 task ID、每任务轮换的独立 check authority 和完整快照，包含规范化 argv、实际 cwd、kind、returncode、目标、有界输出、spill 引用、执行完整性、工作区稳定性与限制；扩展自报字段会被剥离。任务最多保留 32 条记录，失败按 argv＋cwd 严格替代，且只有宿主确认完整、稳定的成功检查才能清除；淘汰记录不清除未解决失败。
- 写入后的 stale 按检查签名维护，信息查询和无关稳定命令不能复活编辑前证据；SessionRuntime 收尾扫描失败或发现未归属变化时同步标记已有任务检查 stale。目标提取按 unittest/pytest/ruff/mypy 语法排除过滤值和排除项，不再把 `tests` 误写成 `tests.py`。
- Console/TUI 分开呈现文件状态检查与任务验证，均显示记录限制/容量截断，并始终显示“需求覆盖未自动确认”。无关测试、`OK`/`Ran 0 tests` 文本、模型总结、旧摘要和正常 finish 都不能签发业务覆盖；普通无检查任务保持 unverified。D1 Hello、D2 写文件脚本、D3 无关测试均由 fake Provider＋临时工作区覆盖。
- 独立只读复审先后发现并复现：信息查询复活 stale、不可信退出码清除失败、命令目标误报、task 标签复用、TUI 限制缺失、一次性 CLI 收尾未同步失效、内部完整记录越过 32 条。各项均先补失败回归再最小修复；最终复审未发现 Critical/Important。
- 新鲜验证（Windows / Python 3.11.6）：审查补齐后的 Agent/命令/证据/工作区组合 `Ran 223`，OK（2 skipped）；最终全项目 `Ran 1476 tests in 313.928s`，OK（13 skipped）。compileall、CLI `--help`、23 项导入边界与 `git diff --check` 均 exit 0。首次全量曾暴露 2 条依赖旧“普通脚本必为 UNKNOWN”语义的断言，按 A1 计划改为稳定非零退出可重规划后修正；没有把首次失败记作通过。
- 未调用真实 Provider，未读取 `.env.local`、真实密钥或用户会话数据库，未安装依赖、提交或推送。剩余限制：快照是有限覆盖观察而非 OS 沙箱；任务验证只报告事实，不自动推断需求覆盖；记录只随当次 `RunResult` 存在，不跨 Session 持久化。Linux/macOS、Python 3.12、真实 Provider 和手工 TUI 尚未验证。当前停在第一批审查点，B1/B2/C1 未实施。

## 2026-10-06 下一阶段执行方案交接（文档完成，代码待实施）

- 执行方案：`docs/superpowers/plans/2026-10-06-task-verification-clarification-convergence.md`。包含当前代码依据、模块职责、接口约定、S0/A1/A2/B1/B2/C1/D 阶段、验收场景，以及三批 coding agent 提示词。
- 第一批 S0/A1/A2：复核已记录完成的结束协议 P2；拆开命令副作用观察与测试证据签发，解决普通脚本正常退出仍因缺少快照而 UNKNOWN；增加保守的任务验证事实与展示。编写时实际 HEAD 为 `43f721a`，实施仍以最新工作树为准。
- 第二批 B1/B2：独立 ask_user，第一版进程内等待、默认 300 秒超时、可取消，保持 Session/工作区锁；回答不审批，恢复前检查外部编辑，同批后续动作跳过。暂不实现跨重启等待或释放锁后的恢复。
- 第三批 C1/D：有界重复检测及端到端验收。相同失败第 3 次、无变化重复读取第 4 次、A→B→A→B→A 振荡分别停止；保持已有协议预算、安全优先级、已提交变更和失败记忆。
- 任务计划设计与维护继续留在后续阶段。本轮仅创建执行文档和更新交接，没有修改功能代码；没有重新运行功能测试，也没有将历史 P2 测试结果作为本轮复审通过证据。
- 下一动作：用户将执行文档第 13 节第一批提示词交给 coding agent，先完成 S0/A1/A2，交付审查后继续 B/C。各阶段证据放 `runtime/task-quality-round1/`。
- 文档检查：258 行计划的代码围栏、阶段标记、引用的既有源码与测试路径、交接链接检查通过；相关 `git diff --check` 退出 0。仅验证文档结构，不代表上述功能已实现。

## 2026-10-06 后续开发目标与阶段划分（用户已确认，待设计实施）

- 下一阶段聚焦三项：**任务验证、需求澄清与等待、失败收敛**。这是优先级与范围记录，不代表功能已实现，也不自动启动代码修改。
- **任务验证**：将任务目标、实际变更、验证命令及其结果关联起来；区分文件创建、语法检查、指定测试通过和功能验收。无关测试通过不能作为本次需求完成的依据，无法证明的覆盖应明确显示为未知或未验证。
- **需求澄清与等待**：支持提出具体问题、等待用户回答后继续，并区分等待、失败与成功。等待期间可取消；若释放工作区锁，恢复前必须重新获取锁并检查工作区变化。用户回答不能自动变成后续写入或命令的审批。
- **失败收敛**：在现有结束协议预算之外，识别相同代码状态下的重复失败、无效重复操作与修改来回抵消；引导最小诊断、针对性修复和验证，达到预算后带证据停止或请求澄清。不能为通过而自动降低测试预期，也不能以“执行过任意工具”作为有效进展。
- 建议实施顺序：先建立任务验证事实与展示，再实现澄清/等待生命周期，最后基于前两者实现失败收敛。各项单独形成可验收的执行文档，小步实施；具体数据结构、阈值和接口尚待设计。
- **后续提升阶段**：任务计划的设计与维护，包括基于项目探索制定计划、步骤状态、完成依据、阻塞原因与计划调整；不在上面三项中顺带引入复杂规划器或强制五阶段流程。
- 前置检查：结束协议 P2“提前停止后的历史闭合与记忆兼容”仍需确认修复并复审；不能以新增能力替代该遗留问题的验收。
- 本轮只记录目标，没有修改源码或运行功能测试。下一动作：在最新代码与 P2 状态明确后，为“任务验证”编写具体实施方案，并保留三项之间的接口约束。

## 2026-10-06 ReAct 结束协议复审 P2 修复（已完成）

- 根因确认：第三次 native 无工具响应只返回失败结果，没有在原始会话历史中写入闭合表示。最后一条 assistant 文本既不是完整工具回合，也没有反馈配对；旧失败任务因此阻断 `plan_compaction` 与 `plan_save_candidate`。普通 `protocol_feedback` 又会被摘要器当作噪声，且不能单独证明零工具任务已经终止。
- RED 证据：零工具用例期望末条为 user 终止标记、实际仍为 assistant；“list_files → 三次文本 → 同一上下文正常 finish”用例期望摘要器调用 1 次、实际为 0。取消/审计失败与孤立 ToolCall 的保护用例在修复前保持通过，证明问题限定在专项终止提交与历史闭合。
- Runner 现在只在第三次响应的 invalid_action 审计成功、且再次确认未取消之后，追加固定 `task_termination` user 消息。内容明确“本轮已停止、任务未完成、结果仍需确认”，不要求下一轮继续调用工具；没有伪造 finish、tool result、验证通过或业务完成。失败任务不推进 `latest_completed_task_seq`，也不触发成功收尾或额外 Provider 请求。
- `ContextManager` 只把“无 tool_calls 的 assistant + 精确程序终止标记”识别为失败终止组。任务全部消息必须被完整分组，终止组必须位于末尾；任何真实 ToolCall 缺少匹配结果时仍拒绝压缩和保存。该规则同时覆盖停止前已有完整工具回合及完全没有工具回合的任务。
- 压缩提交和保存候选合并会根据可信终止标记确定性加入一条 task 作用域的 pending 待办，来源只引用标记消息序号。摘要模型被要求不要重复生成或把它解释成权限、批准、验证或文件状态；即使 fake 摘要器返回空语义候选，失败事实仍会保留。容量、来源、generation、覆盖边界和原子提交校验保持原样；合并阶段的校验拒绝会转换为现有 `MemorySummaryError(code="commit")`，保留原历史及已经发生的摘要用量。
- 新鲜验证（Windows / Python 3.11.6）：结束专项 20 项、记忆压缩 9 项、全部记忆测试 87 项、全部 Agent 测试 153 项、SessionRuntime 64 项、协议 10 项均通过；内存语法检查与 `git diff --check` 退出 0。最终完整项目 `Ran 1447 tests in 215.891s`，OK（13 skipped）。未调用真实 Provider、未读取 `.env.local`、真实密钥或用户会话库，未安装依赖、提交或推送。
- 剩余限制：本修复只识别新版本生成的专项终止标记；已存在且缺少该标记的旧进程内历史不会自动迁移。每个失败终止任务会占用一条结构化待办，达到既有每区 20 条容量时摘要按原规则保守失败并保留原历史，需要用户审阅、完成或归档条目。真实 Provider、Linux/macOS、Python 3.12 与手工 TUI 展示仍未验证。

## 2026-10-05 ReAct 结束协议修复（S0—S4 已实施）

- 执行文档：`docs/superpowers/plans/2026-10-05-react-termination-recovery.md`；阶段和 RED/GREEN 证据位于 `runtime/react-termination-recovery/`。本轮以 `de0af48` 和已有未提交工作树为事实基线，保留了常用文件工具第一轮的全部改动，没有从 HEAD 覆盖重叠文件。
- 系统提示、原生纠错反馈和 `finish` 工具定义现在明确要求：完成、无法继续或需要用户信息时必须显式调用 `finish(summary=...)`。普通文本、完成字样和 `finish_reason=stop` 不会成为成功证据；正常 `finish` 仍经过既有本地验证、取消、清理和 UNKNOWN 判定。
- `AgentRunState` 新增单任务累计计数；仅 native 的 `ToolCallCountError` 且无 actions 会计入。第 1/2 次反馈 1/3、2/3，第 3 次在保留该轮 usage、assistant 历史和审计后，经统一 finalizer 以未完成停止；普通工具、写入和测试成功不清零，新任务及不同 Agent 实例从 0 开始。`legacy_json`、重复 call ID、普通工具失败与 max_rounds 保持原语义。
- 专项停止不会调用成功收尾或生成成功记忆候选，也不会撤销已提交文件、目录、账本或验证证据。真实文件工具 + SessionRuntime 集成测试证明账本正常封存、工作区锁释放且下一任务可以继续；取消和审计失败仍保持更高优先级。同批 `finish` 后续动作仍只补 `skipped` 结果，不执行写入。
- 审计新增固定 `native_missing_tool_call` 原因码、计数、上限和 `will_stop`；仅该预定义原因码允许原样写入，任意自由文本 `reason` 继续按字符数脱敏。专项停止固定摘要不会记录模型正文或源码。
- 新鲜验证（Windows / Python 3.11.6）：结束专项 17 项、协议 10 项、Agent 邻接 149 项、SessionRuntime 64 项、tools 97 项、audit 6 项均通过；最后代码状态的完整项目 `Ran 1440 tests in 199.758s`，OK（13 skipped）。未调用真实 Provider、未读取真实密钥或用户会话库，未安装依赖、提交或推送。
- 剩余限制：这是 native 无工具响应的有界纠错，不是通用无进展检测；不会识别重复工具、测试失败振荡或业务覆盖不足。真实 Provider 调用 `finish` 的改善幅度、Linux/macOS、Python 3.12 和手工 TUI 展示仍未验证。

## 2026-10-05 常用文件工具第一轮（S0—S5 已实施）

- 执行方案：`docs/superpowers/plans/2026-10-05-filesystem-tools-round1.md`；阶段证据位于 `runtime/filesystem-tools-round1/`。实现基线为 `de0af48`，保留了开始时已有的 `project.md` 与计划文档改动。
- 新增内置 write 工具 `create_directory`；`parents`/`exist_ok` 默认 `true`，单次最多 16 层、单任务最多 128 个目录。`create_file` 增加可选 `create_parents`，默认 `false` 保持旧行为；`apply_patch` 仍要求先建立父目录。未放宽 `run_command` 白名单。
- 目录使用独立快照、identity、账本、副作用和 Runtime 状态。审批前安全祖先绑定跨审批持有，随后按 Windows 句柄链或 POSIX `dir_fd` 逐层下潜；审批后替换、链接/reparse point、身份变化、补偿不完整和临时发布不确定性均保守拒绝或升级为 UNKNOWN。
- 撤销先全量核对文件、目录身份和内容所有权，再恢复文件并最深层移除本任务创建的空目录。中途失败后若可完整补偿，会以新 identity 刷新最近账本和工作区基线，并立即撤销旧验证 authority；用户新增内容、替换目录和既有父目录不会被递归删除。
- fake Provider 端到端覆盖目录、实现与测试文件、真实 unittest、finish、整组撤销、下一任务无需虚假外部修改确认，以及 native/legacy、同步/异步和三条创建路径。纯目录任务可完成但验证保持“未运行”，目录变化不能伪造通过或清除失败/UNKNOWN。
- 新鲜验证：目录/账本/工作区/Session 组合 `Ran 325`，OK（3 skipped）；Agent/Provider/Eval/协议/CLI/TUI 组合 `Ran 230`，OK；最终完整回归 `Ran 1420 tests in 203.884s`，OK（13 skipped）。188 个 Python 文件内存语法检查、CLI `--help` 与 `git diff --check` 退出 0；独立复核无 Critical/Important。
- 未调用真实 Provider、未读取 `.env.local`、真实密钥或用户会话库，未安装依赖、提交或推送。Windows 目录符号链接用例因当前账户权限跳过；Linux/macOS、Python 3.12、手工 TUI 与真实 Provider 仍未验证。目录撤销账本和 `modified_directories` 只在当前进程有效，不跨重启持久化。

## 2026-10-05 Windows CI 修复（本地完成，等待远端验证）

- 按用户要求取消 Linux CI，保留 Windows Python 3.11/3.12。新增 `test` 可选依赖 `setuptools>=68`，CI 安装 `.[test]`，解决包发现测试缺少构建工具的问题；未在本机安装依赖。
- 敏感路径扫描测试的 guard 改用规范化根路径，避免 Windows 临时目录别名等路径写法差异导致 relative_to 抛 ValueError 并被包装成 io_error。用包含 `..` 的等价目录稳定复现旧失败；关闭路径排除的反向验证仍触发敏感文件断言，没有放宽安全检查或修改生产扫描逻辑。
- 在 `runtime/ci-windows-fix` 独立 worktree（`codex/fix-windows-ci`，基线 `47d08af`）验证后，将 CI、pyproject、测试和 README 的最小补丁回填当前工作区；命令兼容任务的既有改动保留。本次未修改 src、未提交或推送。
- 隔离完整回归：Windows / Python 3.11.6，1355 tests，OK，11 skipped，212.969s。隔离与回填后聚焦回归均为 22 tests、OK、1 skipped；185 个 Python 文件内存语法检查、CLI --help、git diff --check 通过。完整回归不包含主工作区另行进行的命令兼容修改。
- 证据：`runtime/ci-windows-verification.md`。本机没有 Python 3.12，未执行新环境依赖安装；本次提交包含该修复，推送后仍须检查两组 Windows CI。Linux inode 重用相关测试问题未修复，恢复 Linux CI 前需要处理。

## 2026-10-05 命令兼容与错误恢复（S0—S5 已实施）

- 执行文档：`docs/superpowers/plans/2026-10-05-command-compatibility-and-recovery.md`；阶段证据位于 `runtime/command-compatibility/`。实现基线为已经提交并推送的 `47d08afb02f421f6130ea963ffcf33a4f1b9efad`，本轮改动由本次提交收口。
- 六个 Python 请求别名统一到当前会话可信解释器；固定版本查询归一化为隔离参数且不产生 UNKNOWN 或验证证据。脚本、unittest 目标、discover 路径与审计均按同一个单次有效 cwd 解析，不修改共享策略。
- unittest 单段简写只映射到 cwd 内已存在的 `.py` 普通文件。两个枚举化命令形式错误可返回固定脱敏的 `INVALID_ARGUMENT / REPLAN`；直接测试工具先经过等价 `python -m` 的完整安全校验。越界、敏感路径、禁止选项、未知程序、拒绝、取消与未知副作用继续停止，没有全局降级 `POLICY_DENIED`。
- native/legacy_json 共用命令说明。审批显示原请求、归一化 argv、有效 cwd 与超时；Console 标题区分补丁、命令、扩展、MCP 和工作区门禁。strict/relaxed/fullaccess、read_only 与危险 MCP 边界保持既有语义。
- 合成计算器使用 fake Provider 和当前虚拟环境的真实 Python 进程验证：native/legacy 成功路径获得真实 unittest 输出和有效工作区证据；错误实现测试失败、修改后仅查版本均不能被 finish 文案提升为成功；同步与异步工具入口一致。未调用真实 Provider、未读取真实密钥或用户会话数据库。
- 最终 Windows / Python 3.11.6 新鲜回归：`Ran 1384 tests in 263.940s`，OK，12 项条件跳过；186 个 Python 文件内存语法检查、CLI `--help` 与 `git diff --check` 均退出 0。独立审查最初发现兼容构造的 CommandPolicy scoped view 未重新过滤目标工作区 PATH，新回归先证明会命中工作区 fake `git.exe`，修复后复核关闭；当前命令实现无剩余 Critical/Important。未验证 Linux/macOS、Python 3.12 和真实 Provider 端到端质量。并行出现的 Windows CI/pyproject/快照测试修改由上方独立任务记录，不归入本轮命令改造。
- 提交前合并工作树复验首次发现两个真实 MCP stdio 用例在当前 Windows 机器上超时：fixture 仅导入 SDK 后立即退出已需 1.83—1.90 秒，而测试初始化预算为 2 秒。只将这两个测试的初始化/清理预算放宽到 5 秒、外层看门狗放宽到 7 秒，未修改生产超时或错误分类；聚焦 `2/2` 通过，随后完整回归 `Ran 1384 tests in 416.355s`，OK，12 项条件跳过。

## 2026-10-05 第二轮模块整理（S0—S5 已完成）

- 执行文档：`docs/superpowers/plans/2026-10-05-module-organization-round2.md`；实施证据位于 `runtime/module-organization-round2/`。本轮以第一轮未提交工作树为事实基线，没有用 HEAD 覆盖既有改动，也未重做第一轮。
- 14 个唯一实现已分别迁入 `tricoder.process`、`tricoder.workspace`、`tricoder.session`、`tricoder.presentation`；原根模块现在是显式薄转发层。内部生产代码和常规行为测试使用规范新路径，新旧常用类型、函数、异常与状态对象保持对象同一性。
- Eval 隐藏进程 helper 不再假定依赖文件同目录，而是分别定位 `process.control`、`task_cleanup`、`core.cancellation` 的真实源码。隔离测试清空 `PYTHONPATH`、使用 `-S -B`、放置恶意同名包，并真实执行成功命令和超时清理命令。
- 导入守卫覆盖 14 个旧入口、绝对/包级/相对/动态导入形式、四个轻量包、不同冷导入顺序、MCP/tools 与 Agent 边界；`session.store` 不会带入 Runtime/Agent/TUI，`presentation` 包不会提前加载 Textual。第一轮记忆异常进度、finally 单次合并和两实例隔离测试保持通过。
- 聚焦验证：process/Eval/MCP 相关 145 项通过（隐藏 helper 组 1 项按符号链接权限跳过）；workspace 138 项通过（6 项平台/权限跳过）；session 221 项通过；presentation 126 项通过；最终模块/Agent/MCP 导入守卫 36 项通过。各集合有交叠，不能相加为全量总数。
- 最终 Windows / Python 3.11.6 新鲜回归：`Ran 1355 tests in 289.426s`，OK，11 项既有平台/权限跳过；compileall（缓存仅写 `runtime/module-organization-round2/pycache`）、CLI `--help` 与 `git diff --check` 均退出 0。
- 分发产物未验证：当前环境没有 `build` 和 `wheel`，且 setuptools 65.5.0 低于 `pyproject.toml` 的构建要求 68；按授权未安装或升级依赖，因此没有声称 wheel 验收通过。Linux/macOS、Python 3.12、手工 TUI 和真实 Provider 也未验证。
- 新结构说明：`docs/framework/module-layout.md`。本轮没有拆分 `SessionRuntime` 内部状态机、扩展 Provider、改变 SQLite schema/CLI 参数/审批边界，也没有提交或推送。

## 2026-10-05 第一轮补齐再次复审（通过，第二轮可开始）

- 对照补齐前有限源码基线审查 coordinator/loop/agent；F1 的依赖边界、窄字段合并、异常部分提交与 F2 的两实例隔离测试均已落实，未发现阻塞性问题。
- 本次实际运行 `.venv/Scripts/python.exe -B -m unittest discover -s tests -q`：1337 tests，OK，11 skipped，301.275s，退出码 0。运行中有既有 asyncio/Textual 慢回调诊断；没有为测试通过修改源码或断言。
- 独立只读复核完成 52 个纯内存基线对照场景，状态、审计事件及异常身份一致，覆盖取消、KeyboardInterrupt/SystemExit 和回调异常；未调用真实 Provider。
- 复审内容标识与环境限制保存在第二轮计划第 0 节。第二轮文档已更新前置状态、记忆异常进度/隔离回归要求，以及新目录下导入守卫的适配要求。
- 下一步：将 `docs/superpowers/plans/2026-10-05-module-organization-round2.md` 第 8 节提示词交给 coding session，按 S0—S5 实施。本次未修改功能代码、未启动目录迁移、未安装依赖或提交。

## 2026-10-05 第一轮重构审查补齐（C0—C4 已完成）

- 执行文档：`docs/superpowers/plans/2026-10-05-agent-refactor-round1-review-fixes.md`；实施证据位于 `runtime/agent-refactor-round1-review-fixes/`。本轮以未提交的第一轮工作树为事实基线，没有从 HEAD 覆盖用户改动，也未启动第二轮目录迁移。
- F1 已补齐：`MemoryCoordinator` 不再导入 engine 或接收 `AgentRunState`。Runner 通过不可变 `MemoryStepInput` 传入 Session 快照、完整固定前缀与工具定义；协调器返回窄 `MemoryStepResult`，异常进度由每次调用独享的 `MemoryStepProgress` 保存，Runner 在统一 `finally` 中单点合并。
- 异常语义保持：摘要调用前计数、收到 usage 后计量、审计成功后才发布压缩/候选。第一批压缩成功后第二批摘要失败、审计失败或原生取消仍保留第一批；`on_error`/`log` 抛出的首异常保持对象身份；保存候选全批与候选审计通过前不发布中间候选，也不擅自提前合并旧实现未统计的调用/usage。
- F2 已补齐：新增两个独立 Agent 的 `asyncio.Event` 确定性交错测试，覆盖消息、call ID、候选、业务/记忆用量、工具计数、显式父令牌取消和 `Task.cancel()` 隔离；另固定工具调用事件后断流、缺少完成事件及 event sink 首异常三个 Provider 边界。未实现或声明同一 `CodingAgent` 实例并发支持。
- 阶段证据：C0 基线 Agent 113、记忆 84、跨边界 122（跳过 1）项通过；C1 表征 6 项、C2 最终聚焦 34 项、C3 新专项 6 项及异步组合 23 项通过。独立复审最初发现导入守卫绕过（Important）与交错测试无界等待（Minor），均已补回归并由同一审查 Agent 复核关闭；当前无 Critical/Important。
- 最终 Windows / Python 3.11.6 验证：`Ran 1337 tests in 309.134s`，OK，11 项既有平台/权限门禁跳过；本轮新增测试无跳过。compileall 与 `git diff --check` 通过。未调用真实 Provider，未验证 Linux/macOS、Python 3.12 或同一 `CodingAgent` 实例并发；运行中保留了既有 asyncio/Textual 慢回调诊断。第二轮目录整理的前置条件现已满足，但本轮没有启动第二轮。

## 2026-10-05 第二轮目录整理交接（待实施，前置复审通过）

- 执行文档与 coding session 提示词：`docs/superpowers/plans/2026-10-05-module-organization-round2.md`。
- 范围：将 14 个实现模块分组迁入 `process/`、`workspace/`、`session/`、`presentation/`；旧路径保留薄兼容模块，内部使用新路径。保持业务行为，不拆 SessionRuntime 内部逻辑，不扩展 Provider。
- 第一轮现已产生 `engine/` 与 `context/coordinator.py` 等边界；第二轮 S0 仍须以当时最新工作树重新核实 R0—R6 证据、旧导入与 Eval 独立依赖闭包，不能仅依赖本条历史记录。
- 前置补充：`docs/superpowers/plans/2026-10-05-agent-refactor-round1-review-fixes.md` 的 C0—C4 已完成；本轮未启动第二轮。后续开始迁移时仍须重新核对当时工作树和独立依赖闭包。
- 关键风险：Eval 隐藏 helper 按 `__file__` 复制源码；搬目录必须保持其独立运行依赖闭包。另需覆盖实际 patch 查找位置、类型/ContextVar 唯一性、冷导入、锁与快照门禁、分发包资源。
- 本次只写第二轮计划和本交接记录，未改功能代码、未运行功能回归。下一步由 coding session 按 S0—S5 执行，保留现有工作树改动；证据放 `runtime/module-organization-round2/`。
- 文档验证：14 对迁移路径及其源文件、S0—S5 阶段、引用测试文件、代码围栏和空白检查通过；`git diff --check` 通过。这些检查不代表源码重构已实施或功能回归已通过。

## 2026-10-05 Agent 第一轮职责拆分（R0—R6 已实施）

- 执行文档：`docs/superpowers/plans/2026-10-05-agent-refactor-round1.md`。
- 范围：保留 `agent.CodingAgent` 入口，拆分历史、Provider 消费、观测审计、单任务状态、记忆协调、工具批次与结果收尾；本轮不整体搬目录、不重构 SessionRuntime、不新增 Provider 或断流恢复。
- 实施结果：`agent.py` 从 1857 行的综合实现缩减为公开门面；单任务编排、状态、Provider 收集、观测审计、工具批次和收尾分别落在 `engine/`，历史与记忆协调分别落在 `context/history.py`、`context/coordinator.py`。
- `CodingAgent` 的构造参数、同步/异步入口、`context_manager` 与现有公开导入保持兼容；内部执行每次创建独立 `AgentRunState`，没有跨任务共享消息、计数、验证或记忆候选。
- 保持的关键边界：工具副作用先发布再构造/通知；批次首次停止后剩余调用只补 `skipped`；取消、清理、审计首异常、验证 authority、`unknown_effects`、消息配对、记忆覆盖水位和业务/记忆用量分离均由既有专项回归覆盖。
- 本轮未重构 `SessionRuntime`、providers.py、数据库/CLI 配置，也未改变 Session/工作区锁和快照门禁；没有调用真实 Provider、读取真实密钥或安装依赖。
- 实施证据与阶段日志：`runtime/agent-refactor-round1/`。完整结果及剩余限制见其中 `progress.md`；本地验证不能替代 Linux/macOS、Python 3.12 或真实 Provider 验证。
- 最终 Windows / Python 3.11.6 确认：全量 1318 tests，OK，11 skipped；Agent 113、批次 24、记忆 84、验证/错误/副作用 131、Eval 118、工作区 37、导入/MCP 边界 16 均有独立日志。compileall 与 `git diff --check` 通过；独立复审无 Critical/Important。一次全量曾在 BaseException 压力用例中出现 Windows 锁非稳定失败，目标用例 6 次独立复跑及随后全量均通过，详见实施记录，未据此修改锁语义。

## 2026-10-04 工作区锁与任务前快照确认（W1—W6 已实施）

- 任务文档：`docs/superpowers/plans/2026-10-04-workspace-lock-snapshot-gate.md`。
- W1：新增跨进程 `WorkspaceLock`。锁只按规范工作区根确定，和 Session ID、
  会话数据库无关；Windows 真实子进程已验证同根竞争、异根并行、不同数据库同根竞争。
  内部锁另有稳定用户级守卫；Linux 实现抽象 socket 内核锁以避免两个文件系统锁名同时被
  替换后形成 split-lock。空闲入口不持锁；活动标记仅在真实执行前建立，崩溃遗留必须人工
  确认资源已结束后恢复。
- W2/W3：新增有界双清单内容快照与三分支门禁。无变化直接执行；有变化完整分页展示，
  明确确认后再次全扫；再次变化重新确认。扫描失败、超限、不稳定、无确认器或取消均在
  Provider/工具/待执行消息之前阻断，`fullaccess` 与只读任务也不能绕过。
- W4：每个 Session 的源码基线仅驻当前 Runtime 内存；切换/关闭即清除。恢复会话无基线时
  明确确认初始化。外部变化撤销与当前代码不匹配的验证证据，并向 Agent 注入不含源码正文的
  临时安全提示；语义记忆、审批、`unknown_effects` 和原始对话保持原语义。
- W5：单次 CLI、Shell/TUI 的 `SessionRuntime` 路径、撤销和 Eval 合成副本均接入门禁。
  撤销预览持有工作区锁，提交前复扫绑定快照；拒绝或旧预览不会写入。任务末仅在结果成功、
  账本可完整解释且无 taint/未知效果时更新基线，否则保留待确认差异。撤销成功还会用工具
  返回的真实发布后 inode/内容/权限证据做收尾归因；Eval 的 Runtime 清理返回 `False` 时
  保留诊断数据库并阻止进入验证。
- W6 新鲜验证（Windows / Python 3.11.6）：锁专项 `Ran 13`，OK（4 项平台/权限跳过）；
  CLI/TUI/Eval/门禁组合回归 `Ran 225`，OK（5 项平台/权限跳过）；最终完整回归
  `Ran 1306 tests in 308.253s`，OK（11 项平台/权限跳过）；`compileall -q src tests` 与
  `git diff --check` 均通过。
- 实施证据：`runtime/workspace-consistency-implementation/verification.md`。未读取密钥、
  `.env.local` 或真实会话库，未调用真实 Provider、安装依赖、恢复 Docker、提交或推送。
  Linux/POSIX、网络文件系统、Python 3.12、手工 TUI 交互和非协作外部进程仍未实测。

## 2026-09-28 无占用入口与 Session 延迟创建（任务 1—5 已实施）

- 文档：`docs/superpowers/plans/2026-09-28-session-landing-lazy-creation.md`。默认
  `chat`、裸 CLI 与 TUI 现在进入纯内存入口：不恢复 latest、不生成伪 `default` 行、
  不取得 Session 锁，也不装配 Provider/Agent。
- 首次非空普通任务在任务互斥区内创建、自动命名、锁定并发布唯一 UUID Session；
  任务正文不参与名称生成。首次创建提交前取消/退出不留行，提交后 Provider 失败保留
  已创建 ID，避免静默重试为另一个会话。
- `initial_session_id` 是唯一显式恢复入口。真实 Session（包括历史名称 `default`）继续
  跨进程互斥；被占用的历史会话不会降级到 latest、新建或使交互入口退出。
- Shell/TUI 使用可空 Runtime 状态投影。入口中的模型和权限是首次任务待用草稿；需要
  真实 Session 的命令返回稳定提示。TUI 用完整 UUID 作为选项值，名称和遇到碰撞会延长
  的 ID 前缀只作展示，同名/同模型不会串会话。
- Eval 重启在关闭前保存完整活动 ID，并显式恢复同一 ID；按名称切换匹配到多条记录会
  拒绝歧义，完整 UUID 仍精确匹配。
- 聚焦证据：入口/Runtime/所有权/CLI/Shell/UI/Eval `Ran 182`，OK；迁移后记忆/
  Effect 专项 `Ran 64`，OK；Session/可靠性/验证证据专项 `Ran 86`，OK（1 项平台跳过）。
  Windows 两个真实子进程可同时停在入口且不增行，并发首次提交创建不同 UUID。
- 最终新鲜门禁（Windows / Python 3.11）：`unittest discover -s tests -q` 运行
  `1255 tests in 313.760s`，OK，6 项为既有平台/权限跳过；`compileall -q src tests`
  与 `git diff --check` 均退出 0。Textual/asyncio 的慢回调行是诊断输出，不是失败。
- 未读取 `.env.local`、真实密钥或用户会话数据库，未调用真实 Provider、安装依赖、
  提交或推送。POSIX、手工 TUI 与 Python 3.12 尚未验证。

## 2026-09-28 Session 跨终端独占与切换交接（已完成）

- 用户选择：切换成功后释放旧 Session；再次切回必须重新读取持久化状态，不复用旧的完整消息、未保存语义候选或撤销账本。模型切换仍保留同一个 Session 的上下文与账本。
- 实现：`session_lock.py` 用标准库提供本机非阻塞文件锁，Windows 字节区间锁 / POSIX flock；锁按数据库规范路径与 Session ID 散列区分，位于数据库同级 `session-locks/`，不使用 PID、过期抢占或删除锁文件。数据库 schema 不变，无新增依赖。
- 生命周期：先取得目标所有权再加载记忆、构建工具与清理 spill；成功交接后释放旧锁。占用、候选构建失败、旧记忆保存失败或 pending-clear 未完成均不交出旧会话。遗留任务资源未回收时禁止切换；关闭请求交由任务所有者收尾，正常退出/初始化失败释放，进程终止由 OS 回收。
- 入口：CLI/TUI 显示固定占用提示；Shell 异常及 TUI unmount 清理 Runtime。Eval 模拟重启显式关闭旧实例，异常和 trial 结束也释放资源。既有测试中模拟重启的实例相应补真实 close，不关闭互斥保护来迁就测试。
- 自审修复：普通状态变更即将释放任务锁时，并发 close 原可漏接请求；新增受控线程竞争用例先复现，再将关闭标记检查与任务锁释放置于同一个状态锁交接。新增独占专项 18 项通过（含真实独立进程竞争、正常/强制退出、失败交接、CLI/TUI 与退出竞争）。
- 最终验证（Windows / Python 3.11）：独占专项 18 项通过；最终代码完整回归 `Ran 1233 tests in 313.362s`，OK，6 项既有平台/权限跳过（1227 通过）；证据 `runtime/session-ownership-final-verification.log`。Session 邻接 95 项、记忆 84 项通过；验证证据 75 项中 1 项平台跳过。compileall 与 `git diff --check` 通过。日志中的 asyncio/Textual slow-callback 为诊断，不是失败。
- 边界：只保护同一数据库路径下遵守协议的 Runtime；不同 Session 指向同一工作区仍无工作区锁。POSIX、网络文件系统、旧版进程和脱离管理的外部子进程不在本次 Windows 验证范围内。未访问真实会话库、未读取密钥、未调用真实 Provider、未安装依赖、未提交或推送；既有 Eval 未提交改动保留。
- 后续可在 Linux CI 验证 flock 分支，本次无阻塞项。日常使用要保留语义记忆时，在切换前明确预览确认 `/memory save`；退出或切换不会隐式保存未审阅内容。

## 2026-09-23 Agent 效果量化（E1—E6 已实施，真实质量未测）

- 文档：`docs/superpowers/plans/2026-09-23-agent-evaluation-implementation.md`；证据：`runtime/agent-evaluation-implementation/`。
- E1/E2：新增严格 v2 suite/实验定义、旧 smoke/v1 适配、分维度指标、确定性重复调度、条件轮换、时间/取消预算、独立 trial 状态和逐次原子结果。代码正确、正常 finish 与端到端完成分开；未执行、基础设施错误和缺失 usage 不会被删除或当零。
- E3：普通多轮走 `CodingAgent.run_with_context`；固定保存/重启/切换/撤销动作走临时 SQLite 与 `SessionRuntime` 公开接口。实验的 structured/reviewed-summary 配置真实传入 Agent，摘要调用及独立 token 写入结果；不同重复不共享上下文。
- E4：新增白名单故障注入，区分 Provider 透明传输重试、Agent 工具重规划和审批拒绝；只在实际命中时统计恢复。安全观测记录危险请求、实际执行、重复绕过及合法操作放行，不执行真实攻击代码。
- E5：新增 `evals/quality-v1/` 30 道合成自建题，六类各 5，dev/holdout 为 18/12；正确结果和典型错误结果各运行一次隐藏 verifier，契约分别通过/失败。原 smoke 保留。
- E6：v2 报告包含条件、不可逆实验指纹、quality/contract 与类别分组、恢复/安全/记忆/效率、失败阶段及全部代码；`eval-compare` 默认拒绝不兼容实验，仅允许显式变量并保留未配对、提升和回退。未提供价格表时费用保持 null。
- 新鲜离线验证（Windows / Python 3.11）：最后代码改动后的 Eval 专项 `Ran 114 tests in 35.541s`，OK，2 项既有链接权限跳过；全项目 `Ran 1215 tests in 175.311s`，OK，6 项既有平台/权限跳过；30 题正确/错误各一次共 60 个 verifier 结果全部符合预期；compileall、旧 smoke dry-run、Quality V1 dry-run、`git diff --check` 均 exit 0。测试期间的 asyncio/Textual slow-callback 输出是诊断，不是失败。
- 兼容与边界：默认单次 `tricoder eval evals/smoke` 行为和 v1 报告保留；新能力不新增依赖。Harbor、Inspect AI、Langfuse、Docker 和真实 Provider 调用均未纳入本次；离线 fake/fixture 结果不能表述为 Agent 质量提升。
- 回退：继续使用旧 smoke 单次入口即可绕过实验层；删除新生成的 `runtime/evals/<run-id>/` 不影响会话数据库或项目源码。禁用记忆条件使用 `memory_compaction="off"`、`memory_persistence="off"`。

## 2026-09-22 Docker 真实验证未通过并回退

- 实现提交 `eece357` 曾完成 Docker P0—P5 代码、模拟生命周期测试与本地回归，但从未推送到 `origin/main`。
- 真实验证尝试安装了 Docker Desktop 4.91.0 per-user 版本；安装器 SHA-256 与 Docker Inc. 数字签名验证通过，WSL2 数据盘开始初始化。
- Docker 首次启动的订阅服务协议未被接受，engine 随后退出；因此没有拉取镜像、启动容器或验证真实挂载、断网、资源限制与清理闭环。本次真实 Docker 验收标记为**未通过**，不能沿用模拟测试结论声称真实隔离可用。
- 按用户要求，本节所在提交撤销 `eece357` 的源码、测试和 README 变更，当前产品重新回到默认本地执行、尚未实现 Docker 沙箱的状态。Docker Desktop 进程已停止，但软件保留安装，方便后续由用户本人接受协议后重新测试。
- 后续恢复时可 revert 本次回退提交或重新应用 `eece357`，然后使用合成项目与无秘密镜像重新完成真实验收；在此之前不要发布或启用 Docker 模式。

## 2026-09-21 Docker 沙箱可行性与实施交接（待实施）

- 方案：`docs/superpowers/plans/2026-09-21-docker-sandbox-implementation.md`。
- 结论：架构可行；采用独立工作副本、容器执行、确认后回写。需覆盖 CLI、SessionRuntime、Eval 及 MCP/扩展边界。
- 环境：当前 PATH 未找到 Docker，未验证 daemon、镜像、挂载或实际隔离；未安装、拉取或启动容器。
- 本次仅创建方案并更新交接记录，未修改功能代码或运行功能回归。
- 下一步：按 P0—P5 实施；保留现有未提交修改，先完成可独立实施的接口与模拟测试，再在获授权的 Docker 环境完成真实验收。

## 2026-09-21 会话记忆第二轮审查修复（S1—S3 已完成）

- 文档：`docs/superpowers/plans/2026-09-21-session-memory-review-round2.md`；证据：`runtime/session-memory-review-round2/`。
- S1：新增只由可信成功任务推进的完成水位；保存预览与最终提交统一拒绝覆盖不足/越界候选。`/memory refresh` 只运行无工具摘要、最多两批，不重跑业务工具或直接写库；失败、取消、迟到结果、部分批次和压缩来源断层均不提交。`/memory` 与保存预览显示候选、运行时、已保存 revision 及覆盖范围。
- S2：决策 `old → new` 的同 ID/同替代关系重放保持幂等，归档清理后仍可凭活跃替代元数据识别；冲突替代关系和未知旧 ID 继续拒绝。相同终结语义即使来自新消息来源也不重复归档。
- S3：新增 `/memory archive` 和 `/memory archive delete <条目ID>`。列表只显示 ID/类别/状态与条目、字符容量；删除使用 Session/generation/revision/原始快照/消息序号绑定的精确预览，确认只改当前内存目标，不改活跃条目或可信执行状态，也不自动保存。再次 `/memory save` 后删除才跨重启生效。
- 连续流程覆盖 A 候选→B 摘要失败→保存拒绝→刷新→保存→替代重放→40 条归档→确认删除→再保存→重启。该流程还发现并修复 `summary_max_chars > 6000` 时可保存但按默认上限加载失败的问题；Runtime 现用当前配置读取，SQLite schema 未变化。
- 最终新鲜验证（Windows / Python 3.11.6）：记忆专项 `Ran 84`；Context `Ran 9`；SessionRuntime `Ran 59`；CLI/Shell/TUI `Ran 61`；全项目 `Ran 1180 tests in 186.769s`，OK，6 项为既有 Windows symlink/reparse 权限跳过；`compileall` 和 `git diff --check` 均 exit 0。TUI 测试出现约 0.109—0.125 秒慢回调诊断但无失败。
- 兼容性：语义记忆仍默认关闭；`persistence=off` 不新增摘要请求或加载已保存语义记忆。schema v2 与旧行兼容规则不变，无数据库迁移。关闭功能不能恢复此前已压缩掉的原始历史。
- 真实 Provider 补充验证：OpenAI、DeepSeek、GLM 的真实 Key 配置、网络和 native 工具协议均可用；三家无工具结构化记忆摘要均通过严格解析。DeepSeek 的 `fix-subtract` 隔离 Eval 完整通过。OpenAI 与 GLM 的同一 Eval 都完成正确文件修改且隐藏验证通过，但分别因 8 轮内未调用 `finish`、触发命令策略拒绝而以 `agent_failed` 结束；两家的最小 `read_file → finish` 真实任务均通过。因此不能表述为“三家完整 Coding Eval 全绿”。
- 仍未验证：真实用户数据库、手工交互式 TUI、Linux/macOS、Python 3.12。真实验证未回显/保存 Key；未安装依赖、未提交或推送。
- 回退：把 `[memory]` 的 `compaction` 与 `persistence` 设为 `off` 并新开会话；不要删除数据表或覆盖工作区。已确认保存的数据会保留但不加载。

## 2026-09-20 会话记忆审查修复（R1—R4 已完成）

- 文档：`docs/superpowers/plans/2026-09-20-session-memory-review-fixes.md`。
- 修复前真实复现：编辑预览缺少会话绑定；收尾候选未覆盖最近任务；20 条待办后新增条目合并失败。跨会话问题在 runtime 接口层复现；CLI 同步确认期间没有切换入口，TUI 异步确认窗口由 runtime 绑定校验兜底。
- R1 已完成：编辑和保存预览绑定 Session、generation、原始记忆快照、消息序号及持久化版本；最终提交重新校验候选结构与敏感内容。4 项原始缺陷复现和 2 项提交边界复现均先失败后通过；R1 最终 8 项及 56 项邻接回归通过。CLI 同步确认下未发现直接切换入口，TUI 异步模态期间仍由 runtime 绑定兜底。证据见 `runtime/session-memory-review/progress.md`。
- R2 已完成：运行时压缩摘要与 `review_memory_candidate` 保存候选分离；保存覆盖位置之后的全部闭合任务，包括最后一个已完成任务，同时不删除近期内存历史。输入超限只按完整任务最多拆成两批，两批仍不足时拒绝标称完整保存且不发布部分候选。保存预览使用独立候选，确认后写库，重启再加载为运行时记忆。修复前单/双任务复现失败，修复后记忆专项 50 项通过。
- R3 已完成：schema v2 增加 `state`、`replaces_id` 与有界归档；运行时压缩保持保守合并，待保存候选支持同 ID 更新、待办终结、决策替代和新任务目标归档。每类活跃项上限 20、归档上限 40；超限拒绝并保留旧记忆/历史。`/memory edit` 在 CLI/TUI 中可选择固定枚举状态，编辑待保存候选不会提前替换运行时正式记忆。
- 数据库兼容：v1 行读取时在内存映射为 v2（旧待办→pending，目标/约束/决策→active），不会后台回写；下一次用户确认保存才以 v2 写入。未知版本或列/payload 不一致继续拒绝。
- R4 已完成：新增假 Provider 连续流程，覆盖任务 A→任务 B 修正→模型切换→保存→重启恢复→会话隔离→clear，并断言真实 context/SQLite 状态和调用次数。README 已同步保存覆盖、状态/归档、额外调用和恢复边界。
- 最终新鲜验证（Windows / Python 3.11.6）：记忆专项 62 项通过；全项目 `Ran 1154 tests in 152.584s`，OK，6 项均为既有 Windows symlink/reparse 权限跳过；`compileall` 与 `git diff --check` exit 0。证据见 `runtime/session-memory-review/`。
- 未验证：真实 Provider 摘要质量、Linux/macOS、Python 3.12、真实用户数据库迁移。未安装依赖、未读取密钥/真实数据库、未调用真实模型、未提交或推送。
- 下一步：在非敏感测试会话手工试用 structured/reviewed_summary；根据真实摘要质量决定是否继续优化候选差异展示和归档选择界面。

## 2026-09-19 真实 Provider 会话记忆兼容性修复

- 真实试用确认业务工具任务成功，但收尾记忆候选被严格解析器拒绝；旧 UI 只显示统一警告，无法区分 JSON、截断、超时和 Provider 故障。
- 修复采用低信任语义对象：模型只返回目标、约束、决定和待办，本地注入 schema/revision/generation/covered_through；兼容包住整个响应的单个 JSON 围栏，但继续拒绝附加说明、未知字段、伪造来源和工具调用。
- `MemorySummaryError` 增加固定失败类别；UI 与审计仅记录安全类别，不记录 Provider 原文。失败类别审计本身失败时继续按现有 fail-closed 规则停止且不提交候选。
- `ContextSnapshot` 区分协议噪声归一化与预算裁剪；结构化模式只拒绝真正的预算裁剪。已经纠错并形成完整工具回合的旧任务可作为原子摘要来源，原始 Session 历史不会因请求视图归一化而丢失。
- 新增真实故障形态的回归测试；未读取密钥或真实会话数据库，未调用真实 Provider。最终验证证据记录在 `runtime/session-memory/provider-compatibility-repair.md`。
- 最终验证：记忆专项 36 项、ContextManager 8 项、完整项目 1128 项均通过；完整项目有 6 个既有平台/权限跳过，compileall 与 `git diff --check` 退出码均为 0。

## 2026-09-18 会话记忆改造（P0—P5 已完成）

- 文档：`docs/superpowers/plans/2026-09-18-tricoder-session-memory.md`；唯一目标目录为 `D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli`，不修改或创建迁移副本。
- 设计：结构化任务记忆＋近期完整回合；可信验证、审批和未知影响继续由程序管理。先内存摘要，后可选预览确认持久化，再验证恢复与 clear。
- P0—P5 六阶段、M01—M20 验收场景；默认关闭新能力，未添加依赖，未读取密钥或调用真实 Provider。
- 已检查当前代码存在验证证据与未知影响字段，旧计划的“尚未实施”标题不作为当前功能状态依据。
- P0 已完成：读取规则、README、当前代码和事务/清除/命令路由；确认开始时 `src/tricoder/` 与 `tests/` 无用户未提交修改，既有手工实验与计划改动保持不动。
- P0 新鲜基线：context 8、sessions 17、session runtime 59、cancellation 7 项测试均通过；证据见 `runtime/session-memory/baseline.md`。未调用真实模型。
- P1 已完成：新增结构化记忆模型、严格 JSON/来源/长度校验、保守合并、稳定消息编号及 Agent 字段传播；9 项新增测试与 101 项 Agent 邻接回归通过，证据见 `runtime/session-memory/p1.md`。
- P2 已完成：上下文预算纳入固定消息、工具 schema、输出预留及 token/字符限制；只压缩连续闭合任务前缀，工具调用与结果不拆分；校验成功后原子提交，证据见 `runtime/session-memory/p2.md`。
- P3 已完成：独立无工具异步摘要、15 秒可配置超时、取消/截断/非法 JSON 失败关闭、最多两批总结、低信任请求视图注入和独立 memory usage；默认 off 无新增调用，证据见 `runtime/session-memory/p3.md`。
- P4 已完成：新增独立 `conversation_memory` 表、事务 CAS、合成旧库升级、reviewed-summary 恢复、`/memory` 查看/编辑/保存的精确预览确认。候选文本不写审计，关闭模式不加载也不写语义内容，证据见 `runtime/session-memory/p4.md`。
- P5 已完成：`/clear` 递增 generation、清除消息与目标 Session 的语义行，数据库失败进入 pending-clear 并阻止旧记忆复活；补齐取消、会话隔离、失败重试、长历史对比、README 和 M01—M20 映射，证据见 `runtime/session-memory/p5.md` 与 `runtime/session-memory/acceptance.md`。
- 设计调整：正常任务结束的 reviewed 候选固定保留最近两项完整任务；记忆替换改为审计元数据写入成功后才提交，审计失败沿用既有 fail-closed 语义。SQLite 在初始化时创建空表，但 `persistence=off` 不写入语义行。
- 本次最终验证：结构模型 9 项、记忆专项 30 项、邻接回归 162 项均通过；最终完整项目 `Ran 1122 ... OK (skipped=6)`，跳过项均为既有平台/权限门控，本次记忆测试无跳过。未验证真实 Provider 摘要质量、Linux/macOS 和真实用户数据库迁移。
- 下一步：如需实际试用，先在非敏感测试会话启用 `[memory] compaction="structured"`；确认候选质量后再启用 `persistence="reviewed_summary"` 并通过 `/memory save` 保存。默认配置继续保持 off。

## 2026-09-14 LangGraph 独立副本迁移计划（尚未实施）

- 用户要求保留原代码，在独立副本迁移；本次只编写工程文档。
- 文档：`docs/superpowers/plans/2026-09-14-tricoder-langgraph-safe-migration.md`。
- 拟定副本：`D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-langgraph`；实施时原仓库只读，环境、会话、审计、暂存和测试工作区均隔离。
- 设计：LangGraph 仅替换编排，保留可信工具入口；首版顺序执行、不启用磁盘 checkpoint 或崩溃自动续跑。M0—M5 六阶段，A01—A27 验收场景，包含回退演练。
- 当前未复制代码、未安装依赖、未改 src/tests、未执行迁移测试；已有用户修改保持原样。文档静态检查不代表迁移验收通过。
- 下一步：实施授权后重新检查源工作树，从 M0 审核复制清单开始；依赖安装与 Git 操作按实际授权处理。

## 2026-09-14 可靠性前五项任务计划（尚未实施）

- 用户要求将失败改动追踪、工具错误分类、同批失败传播、取消清理、验证绑定文件状态五项写成完整计划。
- 计划：[2026-09-14-tricoder-reliability-top5.md](D:/MaHong/AGENT_WORKSPACE_V2/projects/tricoder-cli/docs/superpowers/plans/2026-09-14-tricoder-reliability-top5.md)。包含共享状态接口、改动文件、分阶段兼容、SQLite 最小状态迁移、实现步骤、验收矩阵、测试命令、排期、回退及交接。
- 关键决定为拟议设计：失败批次保守停止、未知副作用不静默消失、审批单次结束、验证前后与 finish 前核对文件状态。实施顺序 T1→T2→T3→T4→T5。
- 当前状态：只完成计划；没有修改 src/ 或 tests/、运行项目测试、调用模型、安装依赖或提交。已有 test/ 用户改动保持原样。
- 文档静态核验见 runtime/reliability-top5/plan-verification.json；示例仅做语法解析，非新增接口已实现或测试通过的证明。
- 下一步：用户要求实施时，从 T1 基线与真实残留复现开始；执行前重新核对工作区变化，不将现有待复现风险当作已确认缺陷。

## Status

active

## Goal

Evolve the tricoder CLI from a safe, multi-provider local coding agent into a
complete Agent platform while preserving its permission, audit, workspace,
change-journal, and minimal-persistence boundaries. Current milestone: migrate
Hcode capabilities through the approved native-adaptation roadmap.

## Scope

- `src/tricoder/` source, `tests/`, `docs/`, and README inside this project's
  independent Git repository.
- Shared workspace launcher `capabilities/tools/opencode-v2.ps1` and its test
  `capabilities/tools/test_opencode_v2.py`.

## Non-goals

- No OS-level sandbox implementation in the current migration; human approval
  plus policy remain the primary boundary.
- No Hcode Remote/WebSocket server or full free-text conversation persistence.
- No dependency addition, external binary, or source-code reuse before its
  explicit approval and dependency/license gate.

## Constraints

- Follow workspace and project `AGENTS.md`.
- Do not read `.env.local`, `.local/secrets`, `.local/envs`.
- Do not install dependencies or commit/push without explicit approval.

## Acceptance Criteria

- `python -m unittest discover -s tests` passes under the declared Python 3.11+
  runtime; historical test counts remain recorded below and are not current evidence.
- `python -m compileall -q src tests` passes.
- `workspace.py doctor tricoder-cli` reports no findings for this project.
- CommandPolicy rejects qualified executable paths, direct `pytest`/`ruff`/`mypy`
  invocation, external/absolute/`..`/symlink paths in tool commands, and git
  write/pager/textconv/config-override options; git repo-root boundary preserved.
- relaxed 不自动放行任何代码执行命令（仅 git 只读）；子进程环境剔除敏感凭据变量。
- Protocol abstraction keeps native and legacy_json behavior identical
  (covered by `tests/test_protocols.py`).

## Decisions

- 2026-09-07 已批准 verified-stdio 补救：生产本地 stdio 使用 TriCoder 自持
  transport 和 direct process handle，结构化记录进程退出与自持流/任务关闭证据。
  成功不代表任意后台化/脱离进程组后代已消失，不是 OS 沙盒。
- SDK 日志仅在 task-local、来源验证的精确 logger + 词法绝对 pathname 作用域
  过滤；lifecycle、请求和延迟清理 helper 各自持有租约。适配器绑定 `mcp==2.1.1`；
  升级必须重做能力、日志、生命周期及依赖安全评审。remote/auto-install 不支持，
  Windows 离线证据不代表真实外部 MCP/Provider 或跨平台兼容性。

- Hcode migration uses native adaptation: TriCoder remains the architecture and
  security authority; Hcode is a read-only source of designs, algorithms, and
  test scenarios. Approved design and staged execution live in
  docs/superpowers/specs/2026-09-03-hcode-capability-migration-design.md and
  docs/superpowers/plans/2026-09-03-hcode-capability-migration.md.
- The user selected MIT for TriCoder on 2026-09-03. Root `LICENSE`, `NOTICE`,
  and package metadata now record that decision. Later Hcode adapt/copy work
  must update `NOTICE` with concrete source and target files.
- Phase 0 dependency research recommends official `mcp>=2.1.1,<2.2` and
  `PyYAML>=6.0.3,<7` only as `approve-with-conditions` candidates for their
  later phases. That was the Phase 0 decision; Phase 5 subsequently received
  explicit approval and installed `mcp==2.1.1` on 2026-09-04, including its
  manifest/lock changes. PyYAML remains subject to separate Phase 6 approval.
- The migration includes streaming/runtime foundations, context management,
  MCP, Skills, Hooks, Worktree, and child Agents. Remote and OS sandboxing are
  explicitly excluded from this migration.
- `tools.py` split into a `tools/` package (binding, gitignore, undo, handlers,
  filesystem, search, write, command) aggregated by `ToolRegistry`.
- Provider action parsing extracted into `protocols.py` with an
  `ActionProtocol` registry (native + legacy_json); the agent loop no longer
  branches per protocol, and full-round detection delegates to protocol objects.
- CommandPolicy moved from a growing blacklist to per-tool allow-sets plus
  `--opt=value` path checks; executables are resolved to trusted absolute
  paths via `shutil.which` (approval shows the actual program).
- Direct `pytest`/`ruff`/`mypy` invocation is blocked; only `python -m ...` is
  allowed to avoid Windows cwd hijack.
- `opencode-v2.ps1` verifies the reparse-point chain (junction escape) before
  launching; the traversal variable is named `$probe` to avoid a Windows
  PowerShell `$current` assignment quirk.
- Search/glob are bounded: pattern length, `**` count, scan cap, regex length,
  and per-line length limits; `.gitignore` supports common basename/directory
  rules and is documented as a subset, not full Git semantics.
- Phase 3 Context Manager keeps system/current-task boundaries and complete
  tool rounds, anchors estimates to Provider usage when available, and falls
  back to a conservative UTF-8 estimator without adding a tokenizer package.
- Large tool results live under the Session state root, or for one-shot MCP
  runs under the configured audit directory's `runtime/tool-results/`, with opaque
  references, per-entry/session quotas, link/junction rejection, bounded
  preview reads, metadata-only audit, startup cleanup, and `/clear` cleanup.
- Phase 4 Extension Host owns only provider lifecycle, failure isolation and
  aggregation. ToolRegistry remains the sole execution gateway and freezes
  dynamic schemas with explicit origin/risk metadata and current-context binding.
- Project extension configuration cannot authorize access to process secrets:
  `credential_env` names require a second, process-only
  `TRICODER_EXTENSION_ENV_ALLOWLIST` grant. Real extensions remain inactive.

## Progress

- 2026-09-07 verified-stdio remediation：Task 1/2 已通过控制器独立复审；Task 3
  精确日志隔离、文档与最终离线门禁已完成，最终验收仍由控制器独立复审。
  本轮仅修改 SDK/client 日志边界、测试与文档；未改 manifest/lock/.venv，未提交。
  完整证据与遗留风险见
  `runtime/sdd/2026-09-07-tricoder-verified-stdio-remediation/task-3-report.md`。

- Hcode migration Phase 5 implementation and Task 9 review completed on
  2026-09-06; the final whole-feature fix round still awaits controller review
  and fresh complete-suite verification. MCP is an explicitly
  enabled, task-local local-stdio integration. Each Coding Task rebuilds and
  re-approves its servers; every MCP tool is `dangerous`, stays behind the
  asynchronous ToolRegistry gateway, and is removed during deterministic
  cleanup. Remote MCP and automatic server installation remain unsupported.
  Unsupported Schema, malformed output, timeout/cancellation and SDK failures
  fail closed. The repository-local fake server uses only the official API and
  does not access network, environment values, user directories or files.
- Phase 5 changed files: added `src/tricoder/mcp/{__init__,client,manager,
  models,runtime,schema,sdk,security,tool_adapter}.py`,
  `tests/fixtures/fake_mcp_server.py`, and
  `tests/test_mcp_{client,dependency_boundary,integration,manager,runtime,
  schema,security,tool_adapter}.py`; modified `pyproject.toml`,
  `requirements.lock`, `src/tricoder/{config,cli,session_runtime,ui}.py`,
  `src/tricoder/tools/{__init__,command,handlers}.py`,
  `src/tricoder/extensions/host.py`, `tests/test_{config,cli,session_runtime,
  tools,extension_host,ui,context_spill}.py`, `README.md`, `project.md`,
  `docs/framework/mcp-integration.md`,
  `docs/open-source-assessment.md`, and the two MCP/migration plans under
  `docs/superpowers/`. `NOTICE` is unchanged: this phase did not materially
  copy third-party implementation code; the fake fixture only invokes the
  official MCP API.
- The approved direct dependency is `mcp==2.1.1`. `requirements.lock` records
  only the observed Windows / Python 3.11.6 resolution; it is not proof of
  Linux/macOS, Python 3.12, hash-pinned, or universal reproducibility. The
  local stdio transport/pipe retains a residual risk for very large single-line
  messages: bounded result handling cannot prove an end-to-end memory bound
  before line buffering and JSON parsing.
- Historical Phase 5 verification before the final fix round on Windows /
  Python 3.11.6: MCP suite passed 110 tests
  in 27.767s with 1 skip; complete suite passed 762 tests in 235.060s with 5
  skips. The run emitted non-fatal asyncio/Textual slow-callback diagnostics
  in fake-stdio and TUI paths; they were retained in the task report rather
  than suppressed.
- Final-fix focused verification on 2026-09-06: 121 MCP tests (1 existing
  Windows permission skip) and 274 adjacent CLI/Session/tool/cancellation/
  Shell/TUI tests passed; compileall and `git diff --check` exited 0. These
  are scoped results, not a fresh complete-suite claim. The controller owns
  final review and complete-suite verification.
- Doctor output is additionally verified on Windows-compatible CP936 and UTF-8
  streams with an explicit empty offline env file and a process-scoped
  synthetic Key: both exit 0 without a network request or Key disclosure.
- Hcode migration Phase 4 completed on 2026-09-03: added the Extension
  descriptor/provider/host contract, collision-safe lifecycle aggregation,
  cleanup and retry for partially started providers,
  dynamic ToolRegistry registration with immutable schemas and origin-aware
  audit, strict default-off extension configuration, and a secret-safe doctor
  view. No real extension or dependency was enabled. Evidence is in
  `docs/migration/phase-4-verification.md`.
- Hcode migration Phase 3 completed on 2026-09-03: `ContextManager` now owns
  token/character request preparation while the old compaction helpers remain
  compatibility proxies. `ToolResultSpillStore` provides Session-isolated,
  bounded large-result storage and `read_tool_result` supplies path-free chunked
  access. SQLite retains only minimal Session data. Evidence is in
  `docs/migration/phase-3-verification.md`.
- Hcode migration Phase 2 completed on 2026-09-03: OpenAI-compatible
  Providers now expose a bounded SSE stream over the existing urllib transport;
  fragmented tool calls become executable only at a complete response boundary,
  and final usage is delivered before completion. `run_with_context_async()` is
  the normative Agent loop while synchronous callers retain a guarded wrapper.
  Cancellation reaches Provider reads/backoff, tool boundaries, managed command
  process trees, SessionRuntime, one-shot CLI, and TUI. Evidence is in
  `docs/migration/phase-2-verification.md`.
- Hcode migration Phase 1 completed on 2026-09-03: added immutable typed
  Provider/Agent events with secret-safe repr boundaries, a thread-safe
  parent-to-child cancellation token, and an atomic multi-dimensional execution
  budget under `src/tricoder/core/`. The 12 new contract tests were developed
  red-green; no existing Agent behavior or third-party dependency changed.
  Evidence is in `docs/migration/phase-1-verification.md`.
- Phase 0 partially completed on 2026-09-03: created the 17-capability Hcode
  source/target/security/test map, verified Hcode provenance and MIT license,
  and completed current MCP/YAML dependency candidate research. Detailed
  evidence is in `docs/migration/hcode-capability-map.md`,
  `docs/migration/phase-0-verification.md`, and
  `docs/open-source-assessment.md`.
- Phase 0 license gate resolved on 2026-09-03: the user selected MIT; no Hcode
  implementation has yet been copied or substantially adapted.
- Phase 0 environment repaired with explicit user approval: the Python 3.10
  `.venv` was preserved under `runtime/env-backups/venv-py310-20260903/`, a
  Python 3.11.6 `.venv` was created, and only existing declared dependencies
  were installed. Current exact baseline is green: 545 tests pass (4 skips),
  compileall passes, and all 3 smoke Eval cases validate in dry-run.
- Phase 0 self-review completed: after explicit user approval, the one
  pre-existing extra EOF blank line in `test/README.md` was removed and the
  whole-repository `git diff --check` now passes.
- Initial Phase 0 baseline before the authorized environment repair: project
  `.venv` was Python 3.10.16 although
  `pyproject.toml` requires Python 3.11+. Exact unittest ran 452 tests and ended
  with 9 import errors plus 2 skips; all 9 errors and Eval dry-run failure share
  the confirmed missing-`tomllib` environment cause. `compileall` passed. No
  code or environment change was made.
- Completed: read-only Hcode/TriCoder architecture comparison and approved the
  native-adaptation target architecture, end-to-end data flow, security
  invariants, phased gates, rollback rules, and cross-session handoff plan.
- Completed: policy P0/P1 hardening + regression tests; launcher junction
  escape check + test; protocol delegation + `test_protocols.py`; glob/search
  resource bounds + gitignore basename fix + test fixes; README updates.
- Completed: Textual TUI (`src/tricoder/tui.py`, `tricoder tui` entry) with
  modal approval, thread-safe event stream, and pilot tests
  (`tests/test_tui.py`, 3 tests). Dependency review recorded in
  `docs/framework/tui-framework.md`.
- Completed: Planner-Executor (`agent.py` planning round 0 + plan injection +
  degrade; `--no-plan`/`TRICODER_PLAN`/`[agent] plan` config; 5 agent + 4 config
  tests). Design in `docs/framework/planner-executor.md`.
- Completed: command registry (`commands.py` `COMMAND_SPECS`, `/help` generated
  from it in both UIs) and `git_diff` tool demonstrating the tool extension
  point (registered in `tools/__init__.py`, 2 new tests).
- Completed: multi-tool-call rounds (native protocol accepts N calls per round,
  executed sequentially with independent approval/audit) and default
  max_rounds raised 12 → 30. Compaction now groups variable-length tool rounds.
  (2 updated + 2 new tests.)
- Completed: `/permission` command (strict/relaxed levels; relaxed auto-allows
  policy-whitelisted read-only/test commands while file writes stay approved).
  Registered in `commands.py`, enforced via `SessionRuntime._effective_approver`,
  wired into shell + TUI. (3 new tests.)
- Completed: TUI arrow-key selection (`OptionListScreen` modal); `/permission`,
  `/session`, `/model` without args open a selectable list (↑/↓ + Enter/Esc).
  (2 new pilot tests.)
- Completed: TUI collapsible round blocks (each tool round folded into a
  `Collapsible` with a tool-summary title; task/result lines stay visible).
  (1 new pilot test.)
- Completed: `/permission fullaccess` level — auto-allows all non-dangerous
  tools while keeping CommandPolicy/read-only/sensitive-path hard boundaries;
  extensible `_DANGEROUS_TOOLS` set for future delete/rename tools.
  (1 new test.)
- Completed: workspace script execution — `python <relative .py script>`
  allowed by CommandPolicy (relative, no `..`, no absolute, `.py` only);
  auto-execution gated by permission level (fullaccess auto, else approved).
  (2 new tests.)
- Completed: TUI sidebar (double-column layout, live session/state panel,
  thread-safe refresh) + collapsible rounds; fixed sidebar refresh on UI-thread
  command handlers.
- Completed: git boundary protection — git read-only commands are refused when
  the workspace is a subdirectory of a git repo (git would read the repo root
  history/sources outside the workspace). (1 new test.)
- Completed: permission is persisted per session (`SessionMemory.permission_level`
  in SQLite with schema migration); sidebar refresh bug fixed (mis-indented call
  in the permission selector callback). (2 new tests.)
- Completed: P0/P1/P2 security hardening (uncommitted, review before commit):
  - relaxed 不再自动放行任何代码执行命令，仅受限的 git status/diff 元数据查询
    由策略分类后在工具层 auto-approve；show/log/补丁正文仍需审批；fullaccess
    保留自动执行但文档明确非沙盒；子进程环境过滤 API Key/token/
    password/secret/credential 等敏感变量（`_filtered_env`）。
  - CommandPolicy 接受 workspace，所有路径参数经 WorkspacePolicy 真实解析
    （符号链接/junction/存在性/敏感段）；unittest/compileall 建立允许集；
    git 每个只读子命令参数白名单（拒绝 --output/--ext-diff/--textconv/--no-index/
    --git-dir/--work-tree/-C/-c 等）；脚本必须是工作区内存在的普通 .py 文件。
  - TRICODER_BASE_URL 只从进程环境读取，.env.local 不再控制；urlsplit 结构化
    验证（HTTPS、host、拒绝 userinfo/query/fragment）。
  - audit_metadata 区分 `-m <module>` 与 `<script.py>`（execution_kind + 规范化
    相对路径），修复脚本审计 IndexError。
  - 验证状态只由认可的测试/编译/静态检查命令产生（ToolResult.verification_passed）；
    git 只读与普通脚本成功不再标记“通过”，同一修改版本内失败不被任何后续
    成功命令覆盖，新的文件修改会将状态重置为待验证。
  - TUI 动态文本（任务/错误/摘要/diff/session 名/审批详情）一律按纯文本渲染
    （rich.text.Text），固定内部样式才用 markup。
  - SessionRuntime 统一互斥锁：任务启动与 session/model/permission/undo/
    持久化等状态修改原子互斥；审批使用任务启动时的权限快照；完成后只更新
    启动会话。
  - set_permission 采用事务语义，持久化失败时恢复原内存权限；敏感路径覆盖
    .env.*/credentials.*/secrets.*/私钥/服务账号；Provider 响应字节上限
    （超限抛不含正文的 ProviderProtocolError）；finish 非最后时回填“未执行”
    结果保证 tool-result 完整；README 中“沙盒/只读测试/验证通过”描述已对齐。
  - 回归测试：workspace 逃逸 4 条攻击命令、relaxed 不放行代码、env 过滤、
    Base URL 泄露、审计脚本、验证状态绑定、TUI markup 字面、并发锁、
    set_permission 回滚、Git 历史敏感读取、unittest dotted import、compileall
    间接路径清单、Provider 超限、finish 顺序。
- Verified locally: 459 tricoder tests pass (2 Windows symlink skips),
  compileall OK.

## Next Action

- ReAct 结束协议 S0—S4 及复审 P2 历史闭合修复已实现并通过本地完整回归；当前工作树还包含此前常用文件工具第一轮改动，均保持未提交、未推送。下一步由用户决定是否继续人工真实 Provider 验收或统一审查并提交当前工作树。
- 如需补齐分发验收，应另行授权准备满足 `setuptools>=68` 的现有隔离构建环境及 `build/wheel`，
  再从解压 wheel 执行冷导入、CLI `--help` 和 Eval 隐藏 helper 冒烟，不能复用开发树。
- 后续若要拆分 `SessionRuntime`，应另立计划并重新固定锁、取消、记忆异常进度和资源所有权；
  不要把它夹带进本轮目录迁移。

## Later Roadmap

- Confirm the full CI matrix (Linux/Windows, Python 3.11/3.12) once pushed.
- Consider stage-two provider registry consolidation (key_env/base_url/model/
  label/choices in one place) to cut the 5-touchpoint provider onboarding.
- TUI roadmap: `/session` cross-workspace switching, command-output paging,
  and coalescing very small streaming chunks into fewer RichLog entries.
- Planner-Executor roadmap: planning with read-only exploration tools;
  per-task plan persistence for `/diff`/`/undo` context.
- Round-budget roadmap: remaining-rounds prompt injection; stagnation
  detection; per-plan round budget.

## Blockers

- 源码迁移和本地测试无阻塞项。
- 分发产物验收受本机缺少 `build`/`wheel` 且 setuptools 版本低于构建要求阻塞；用户未授权安装
  或升级依赖，因此本轮正确停在“源码与模拟/本地回归通过，wheel 未验证”。

## Eval

- 架构：`evals/smoke/` 是只读、版本控制内的评测定义；每次真实运行只在
  `runtime/evals/<run-id>/` 创建隔离工作副本、结构化结果和报告。
- 隐藏 verifier 决策：Agent 返回并完成修改快照后，框架才把 verifier 注入保留目录
  `.tricoder_eval_verifier/`，执行确定性标准库测试后立即清理；fixture 不包含 Key、
  网络访问或真实 Provider 调用。
- 本轮离线验证：smoke suite 加载、三个 case 的 no-op Agent 均未预先通过，以及
  `tricoder eval evals/smoke --dry-run --no-color`。完整命令证据记录在当前 Eval 任务
  报告中。
- 下一步：真实 OpenAI、DeepSeek、GLM Eval 仅由用户显式手动执行；自动测试不运行
  真实 Provider，也不将离线结果表述为 Provider 质量结论。

## Verification

- 2026-09-07 verified-stdio remediation 历史门禁（Task 3 fix 前），Windows / Python 3.11.6 / `mcp==2.1.1`：
  - MCP 全套：`Ran 172 tests in 41.827s`，exit 0，1 项既有权限 skip。
  - 全项目：`Ran 828 tests in 123.703s`，exit 0，5 skips；没有真实 Provider 或外部 MCP。
  - compileall、`git diff --check` 均 exit 0；pycache 仅写入 remediation runtime。
  - CP936/UTF-8 doctor 使用显式合成 env 文件与进程局部 placeholder，均 exit 0，
    输出不含 placeholder；不发送模型请求。smoke Eval 只 dry-run，3/3 validated；
    `workspace.py doctor tricoder-cli` exit 0、0 findings。
  - 原始测试与命令输出保存在同一 remediation runtime；慢回调诊断未隐藏。
    此后 Task 3 fix 修改 sdk.py 与两项日志测试，仅重跑 73 项 focused，不能把
    上述 172/828 当作 fix 后证据；见 remediation runtime 的 Task 3 fix report。

- 2026-09-07 verified-stdio 最终修复新鲜门禁（最后代码/测试修改之后）：
  - I1–I5 的最终修复与 M1 证据更正已实施，等待控制器独立复审；不认领可提交或发布。
  - focused：113 tests / 32.468s，exit 0、无 skips；MCP 全套：202 tests / 48.040s，
    exit 0、1 项既有符号链接权限 skip；完整项目：858 tests / 131.336s，exit 0、5 项
    既有符号链接权限 skips。完整项目另有 6 条 TUI slow-callback 诊断（0.109–0.110s）。
  - compileall（pycache 写入 remediation runtime）、`git diff --check`、CP936/UTF-8
    离线 doctor 均 exit 0；显式使用本轮 `offline-doctor.env` 假值且输出不含 placeholder。
    CP936 捕获显示替代字符仍是展示限制，不作编码或跨平台完备证明。
  - eval dry-run 3/3 validated；workspace doctor exit 0、0 findings。没有联网、真实
    Provider/外部 MCP、依赖变化、暂存或 commits；HEAD 仍为 `72a501f1`、分支 `main`。
  - 证据及边界见 `runtime/sdd/2026-09-07-tricoder-verified-stdio-remediation/final-fix-report.md`；
    `final-fix-focused.log`、`final-fix-mcp.log`、`final-fix-full.log` 为原始测试记录。
    后续改动仅为本证据文档与报告，不改变以上已验证代码或测试。

- 2026-09-03 Phase 4 verification under Python 3.11.6:
  - focused Host/Registry/Config/doctor/Agent/Runtime suite → exit 0,
    `Ran 387 tests in 23.309s`, OK.
  - `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q` → exit 0,
    `Ran 631 tests in 95.451s`, OK, 4 skipped.
  - compileall → exit 0; smoke Eval dry-run → exit 0, 3/3 validated;
    workspace doctor → exit 0, 0 findings; `git diff --check` → exit 0.
- 2026-09-03 Phase 3 verification under Python 3.11.6:
  - focused Context/spill/Agent/Tool/Runtime/Session/Protocol/Provider suite →
    exit 0, `Ran 309 tests`, OK.
  - `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q` → exit 0,
    `Ran 606 tests in 82.877s`, OK, 4 skipped.
  - compileall → exit 0; smoke Eval dry-run → exit 0, 3/3 validated;
    workspace doctor → exit 0, 0 findings; `git diff --check` → exit 0.
- 2026-09-03 Phase 2 verification under Python 3.11.6:
  - focused Provider/Agent/Session/subprocess suite → exit 0, `Ran 100 tests`, OK.
  - `.\.venv\Scripts\python.exe -B -m unittest discover -s tests` → exit 0,
    `Ran 581 tests in 252.453s`, OK, 4 skipped.
  - `.\.venv\Scripts\python.exe -B -m compileall -q src tests` → exit 0.
  - smoke Eval dry-run → exit 0, 3/3 cases validated.
  - workspace doctor → exit 0, 0 findings; `git diff --check` → exit 0.
- 2026-09-03 Phase 1 verification under Python 3.11.6:
  - focused red run exited 1 with the expected three missing `tricoder.core`
    import errors; the green run passed all 12 new contract tests.
  - `.\.venv\Scripts\python.exe -B -m unittest discover -s tests` → exit 0,
    `Ran 557 tests in 76.192s`, OK, 4 skipped.
  - `.\.venv\Scripts\python.exe -B -m compileall -q src tests` → exit 0.
  - `.\.venv\Scripts\python.exe -B -m tricoder eval evals\smoke --dry-run --no-color`
    → exit 0, 3/3 cases validated.
  - isolated core import boundary check → exit 0; no Hcode, TUI, Session,
    Tools, Provider implementation or Provider SDK import.
- 2026-09-03 refreshed Python 3.11.6 baseline after the authorized environment
  rebuild:
  - `.\.venv\Scripts\python.exe -B -m unittest discover -s tests` → exit 0,
    `Ran 545 tests in 72.607s`, OK, 4 skipped.
  - `.\.venv\Scripts\python.exe -B -m compileall -q src tests` → exit 0.
  - `.\.venv\Scripts\python.exe -B -m tricoder eval evals\smoke --dry-run --no-color`
    → exit 0, 3/3 cases validated.
- 2026-09-03 initial failing evidence retained for diagnosis history:
  - `.\.venv\Scripts\python.exe -B -m unittest discover -s tests` → exit 1,
    `Ran 452 tests`, 9 import errors, 2 skips; confirmed cause is Python 3.10
    lacking `tomllib` while the project requires 3.11+.
  - `.\.venv\Scripts\python.exe -B -m compileall -q src tests` → exit 0.
  - `.\.venv\Scripts\python.exe -B -m tricoder eval evals\smoke --dry-run --no-color`
    → exit 1 at the same missing-`tomllib` import boundary.
- `.venv\Scripts\python -m unittest discover -s tests` → Ran 459, OK (2 skips:
  Windows cannot create symlinks).
- `.venv\Scripts\python -m compileall -q src tests` → OK.
- `python -B capabilities/tools/test_opencode_v2.py` → 3 OK (includes junction
  escape rejection).
- `python -B capabilities/tools/workspace.py doctor tricoder-cli` → no
  tricoder-cli findings after this update.
- `test_workspace_tools` / `check_workspace` fail only on
  `seven-sins-roguelite-codex-starter` (pre-existing, out of scope).
- Not yet verified: Linux/macOS posix binding paths and symlink tests, Python
  3.12, real provider smoke tests (require API keys).
