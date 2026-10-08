# 验证义务复审补齐：F1 / F2 / F3 执行计划

> 执行方式：按 executing-plans 单会话逐项实施，先失败测试、后最小修复；不自动提交或推送。

**Goal：**修复检查范围、零测试与历史未知状态导致的验证义务误清除，保留已修好的恢复会话回顾行为。

**Architecture：**检查的可用性与目标范围由宿主核验；目标必须绑定执行目录；历史未知标记与具体路径可共存。检查执行成功、当前交付成功、历史义务解除是三个不同结论。

**Tech Stack：**现有 Python、SQLite、unittest，无新依赖。

**Spec：**本计划第 1—4 节及 `runtime/verification-obligation/review-findings.md`；上轮计划 `docs/superpowers/plans/2026-10-07-verification-obligation-scope-fix.md` 的安全约束继续有效。

## 1. 范围、证据与不变量

复审基线 HEAD `7c26bd7`。当前工作树可能包含审查文档或用户后续修改，必须保留，不能 checkout/reset 覆盖。实施前重新读规则、README、pyproject、project.md 和相关 diff。

已复现：

| 本轮编号 | 触发 | 错误结果 |
|---|---|---|
| F1 | 根目录 app.py 待验证；只在 sub 中 compileall . | 根目录义务被清除 |
| F2 | app.py 有语法错误且待验证；unittest discover 运行 0 个测试 | 因 exit=0 清除 app.py 义务 |
| F3 | legacy_unknown → 修改 app.py → 单文件检查通过 | 历史来源未知被永久丢弃 |

这三个编号均指“验证义务复审”，不是历史记忆问题的 F1/F2/F3。探针：`runtime/verification-obligation/review-repro.py`，日志同目录 `review-repro.log`。探针 exit=0 只说明运行结束，不能替代正式断言。

必须保留：只读回顾可交付、历史 pending 不因回顾消失、本轮实际写入需验证、净零写入仍有实际效果、UNKNOWN/取消/清理/审计与扫描失败门禁、来源 authority/快照校验、记忆保存确认和以前的失败收尾修复。

不得访问真实 `.env.local`、凭据或用户会话数据库；仅临时 SQLite、合成工作区、fake Provider。不得安装依赖、调用真实模型、提交或推送。

本轮不扩展到摘要提示词、摘要分块、Plan/Replan、沙箱或代码测试覆盖率系统。不把检查目标范围解释成“已证明全部业务行为正确”。

## 2. F1：目标范围必须绑定 cwd

当前 `session/runtime.py:_check_target_covers_path` 仅处理 target/path，`target='.'` 直接返回 True；`_merge_verification_obligation` 丢弃了 CommandCheckRecord.cwd。

实施要求：

- 在宿主检查记录消费边界，将 `record.cwd + target` 统一到工作区相对坐标；不得分别抽取 targets 后丢失记录的 cwd、类型、身份与时效信息。
- `cwd='sub', target='.'` 对应 `sub`；`cwd='sub', target='app.py'` 对应 `sub/app.py`，不能对应根目录 app.py。
- 只有规范化后的工作区根目标才可能表示全工作区范围；字符串点号本身没有该权限。
- 复用 WorkspacePolicy 和现有路径规范，正确处理分隔符、`.`、工作区内合法 `..`、Windows 大小写及边界。非法/无法确定的目标不给解除能力；不扩大命令白名单或允许原本禁止的绝对路径/越界路径。
- 目录祖先比较必须有路径分段边界，`sub` 不覆盖 `submarine`。链接、junction、已消失目标若无法按现有策略证明范围，则保守保留义务。
- 在当前 authority、完整稳定快照和未过期检查的前提下才比较范围；不是“文件快照扫描了全工作区，所以命令也检查了全工作区”。

建议改造现有辅助函数参数，让它消费整个检查记录及必要的工作区策略；不新增一套独立路径安全实现。

## 3. F2：零测试没有解除能力

当前 `_merge_verification_obligation` 用 execution_complete、workspace_stable、returncode==0 等条件筛选，通过记录忽略 `diagnostics=('zero_tests_reported',)`。

实施要求：

- 保留真实 exit=0、输出及诊断，不能把工具执行事实伪造成失败。
- 对宿主签发的 tests 记录，明确报告 zero_tests_reported 时，不允许它解除任何历史已知路径或 legacy_unknown。
- 同一命令也不能单独满足本轮实际修改的验证门禁；避免出现“历史义务保留了，但本轮修改仍被零测试验收”的矛盾。核对 Agent 和 Runtime 的 evidence/报告消费点，统一有效验证条件。
- 回顾/审查可以正常交付并报告“未发现测试”；不能因为这一诊断重新规定所有报告任务都失败。
- 正常有效语法检查可以按现有契约验证其目标；不要对 compileall 等非 tests 命令要求测试数量。
- 缺少测试数量不等于已知零测试。使用已有宿主诊断，不依赖模型总结；不在本轮引入所有测试框架的通用输出解析器。
- 筛选/排除使范围不明时，不能用一个 '.' 自动解除范围未知的旧义务。对于无法证明的范围保留状态并说明限制，不声称取得业务覆盖率。

## 4. F3：未知来源与已知路径同时存在

### 4.1 推荐最小编码：保留现有列和枚举，允许 legacy_unknown 携带路径

无需再增加数据库列。统一约定：

| verification_obligation | pending_verification_paths | 含义 |
|---|---|---|
| none | 必须为空 | 没有保留的义务 |
| pending | 必须非空 | 已知路径仍待验证，没有额外未知来源 |
| legacy_unknown | 可以为空或非空 | 历史未知仍在；还可能有已知路径待验证 |

将 SessionStore 读写中的 `(obligation == 'pending') != bool(paths)` 校验替换成以上三态约束。路径内容仍严格验证，不放开越界、重复、非法类型。同步 dataclass 说明与所有构造/replace/合并分支。

这是兼容读取旧合法数据的语义扩展；旧版本程序可能拒绝新出现的 legacy_unknown+paths 组合，README 应写明该降级限制。不要静默降级成 none。此次只在临时数据库验证，不改用户真实库。已经被旧代码误清掉且没有来源的数据不能凭空重建，应明确记录此限制。

### 4.2 合并规则

先独立维护 `has_legacy_unknown` 和已知路径集合，最后再编码为枚举：

1. 宿主确认新变化：向已知路径集合追加；不能改写 has_legacy_unknown。
2. 合格且范围明确的通过记录：只移除其证明覆盖的已知路径。
3. 局部通过、零测试、回顾、模型文字、结构化记忆编辑都不能清除未知来源标记。
4. 只有符合现有宿主完整工作区检查契约、范围与证据有效且无明确空检查/筛选限制的检查，才可按既有规则解除未知来源。这仍只代表项目定义的文件验证边界，不是业务需求全部通过。
5. 最后编码：未知仍在→legacy_unknown（同时保留剩余路径）；否则有路径→pending；否则→none。

必须搜索并核对 `_merge_verification_obligation`、`_observe_workspace_change`、`_invalidate_verification`、undo/clear、异常与最终失败收尾等所有写入 `verification_obligation='pending'` 的位置，防止仅修一个函数后又被另一分支覆盖。

Console、TUI、/status 对组合状态应显示：`历史来源未知；另有 N 项已知路径待验证`。路径清完但未知仍在时显示未知，不能显示全部通过。历史未知不重新成为本轮纯回顾门禁。

## 5. R0—R4 实施步骤

### R0：三项转为正式失败回归

**Files：**tests/test_verification_obligation.py；复用真实恢复夹具。

- [x] 运行现有 review-repro.py，保留修复前输出到本轮证据目录。
- [x] 新增 F1 测试：根目录 app.py pending，sub/compileall . 不清根目录；关闭重启仍 pending。
- [x] 新增 F2 测试：真实语法损坏 app.py pending，根目录 discover 零测试不清；记录 exit=0 和 zero_tests_reported；关闭重启仍 pending。
- [x] 新增 F3 测试：旧未知→新增真实修改→关闭恢复→局部通过→关闭恢复，未知始终保留，已知路径可被正确移除。
- [x] 三条在修改前出现预期断言失败，不是导入错误、Provider 队列耗尽或环境失败。

### R1：修复检查范围

**Files：**src/tricoder/session/runtime.py；必要时 tools/command.py；tests/test_verification_obligation.py、test_verification_evidence.py。

- [x] 实现第 2 节目标解析，保留整条检查记录的上下文与可信校验。
- [x] 测试子目录点号、子目录同名文件、工作区根检查、合法 ./ 前缀、sub/submarine、非法越界；根目录 pending 与子目录 pending 同时存在时只清后者。
- [x] 明确根目录合格检查仍可解除对应义务，不能一律返回 False 来掩盖问题。

### R2：区分命令成功与有效验证

**Files：**session/runtime.py、engine/loop.py；按需要调整 tools/command.py、task_observation.py 或已有验证辅助函数；相关 tests。

- [x] 零测试记录不参与解除候选；核对本轮修改门禁也不把零测试作为唯一通过证明。
- [x] 测试“纯审查＋零测试”可以交付但保留诊断，“实际修改＋只有零测试”不能验证完成，“零测试＋后续有效目标检查”按后者的真实证据处理。
- [x] 正常 compileall、不相关通过、过期记录、取消/清理失败负向对照继续有效；不修改真实 returncode。

### R3：保留组合状态并贯穿存储/展示

**Files：**session/runtime.py、session/store.py、models.py、presentation/console.py、presentation/tui.py、README.md；tests/test_verification_obligation.py、test_sessions.py、test_tui.py。

- [x] 按第 4 节实施兼容三态约束，验证读写原子性和旧合法行兼容。
- [x] 测试 legacy_unknown+paths 保存恢复；none+paths、pending+空路径、非法路径/枚举/JSON 拒绝。
- [x] 覆盖工具修改与工作区外部变化两个入口，不仅测试 _merge 函数。
- [x] 显式根范围的合格检查、局部检查、零测试、带筛选范围不明检查各有断言；历史未知不能阻塞纯回顾。
- [x] /status、Console、TUI 均呈现未知与已知路径，不显示伪造通过。

### R4：最终验证与交接

- [x] 每个阶段先专项验证；完成后跑相关组合：

```powershell
.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_verification_obligation.py" -v
.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_verification_evidence.py" -v
.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_session*.py" -v
.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_task_outcome_memory.py" -v
.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_agent_termination.py" -v
.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_tui.py" -v
```

- [x] 专项通过后最终完整回归一次：`.venv\Scripts\python.exe -B -m unittest discover -s tests`。
- [x] 重跑 review-repro.py，三项恢复后均不再误清除；原 obsolete_success 对照继续保留 pending。
- [x] `git diff --check`；自查是否把业务验收误当成路径范围，是否误改权限门禁、记忆保存或模型接口。
- [x] 证据保存于 runtime/verification-obligation/review-fixes/，包含修复前后、命令、计数、跳过及限制；更新 project.md、README。
- [x] 交付“修复完成，待复审”，不得自行宣称独立审查通过；不提交或推送。

## 6. 审查重点

1. cwd 不能丢；不能把整工作区快照当整工作区检查执行范围。
2. 零测试 exit=0 是真实执行结果，但不具有解除义务的能力。
3. legacy_unknown 不能因已知路径的增加、清除、外部变化而消失。
4. 原始历史回顾问题继续完成，真正本轮修改/安全异常仍受门禁控制。
5. SQLite 重启后结论一致；不会因只改内存、模型文字或 UI 掩盖问题。

## 7. 可直接交给 coding session 的提示词

```text
请在 D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli 中执行：
docs/superpowers/plans/2026-10-07-verification-obligation-review-fixes.md

先读根/项目 AGENTS.md、README.md、pyproject.toml、project.md 和完整计划，再读 runtime/verification-obligation/review-findings.md、review-repro.py、review-repro.log。

范围仅为“验证义务复审”的三项 P2：
F1 检查目标忽略 cwd，子目录 '.' 被当成整个工作区；
F2 明确零测试 exit=0 仍清除义务；
F3 legacy_unknown 被新 pending 覆盖，之后局部检查误清历史未知。

按 R0—R4 实施，先把真实 SessionRuntime＋临时 SQLite＋fake Provider 探针转为失败测试，再最小修复。F1 要保留检查记录的 cwd/身份/时效并规范化范围；F2 不伪造命令退出码，同时防止零测试成为本轮修改的唯一通过证明；F3 按计划允许 legacy_unknown 携带已知路径，更新存储约束、所有状态写入入口与界面，不新增无必要数据库列。

保留已有恢复后回顾可交付的修复，以及本轮真实修改、UNKNOWN、取消、清理/审计、最终扫描、失败记忆、旧预览失效与保存确认的边界。不要用模型判断或关键词分类解除义务，不清空历史，不靠扩大命令权限绕过。

不处理摘要提示词/分块、Plan/Replan、沙箱或通用覆盖率系统。保留用户已有修改；不要读取真实凭据/.env.local/用户数据库，不调用真实模型、不安装依赖、不提交或推送。

最终执行专项与完整回归，保存证据到 runtime/verification-obligation/review-fixes/，更新 project.md 和 README，说明修复、测试及剩余限制，交付“修复完成，待复审”。
```
