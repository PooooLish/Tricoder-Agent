# quality-v1 真实模型失败归因与改进建议

日期：2026-10-19。范围：独立分析本轮 DeepSeek `deepseek-v4-flash` 的 19 个单次 trial，回答失败来自 Agent、模型还是题库。只分析并保存新验证证据，不实施生产代码、题库或评分修改；Replan 功能开发继续搁置。

## 结论

两方面都有问题。当前 6/19 完整通过率不能当作纯代码能力得分，更不能解释为发现了 13 个 Agent 功能缺陷。14/19 的最终产物通过隐藏测试；题面与评分不一致、工具在合成工作区的可用性、模型生成行为和轮次预算共同影响最终成绩。没有重算旧分数。

以下四组互斥，合计 19 题：

| 组别 | 数量 | 题目与证据 |
| --- | ---: | --- |
| 完整通过 | 6 | sf-subtract、sf-negative-boundary、cf-price-discount、cf-parameter-validation、rc-read-once、rc-provider-transient |
| 产物与收尾正确，仅新增测试文件越界 | 5 | sf-tail-boundary、sf-empty-input、sf-unique-stable、cf-module-interface、cf-backward-api |
| 产物正确，但任务结果失败 | 3 | cf-update-caller、mm-early-constraint、mm-todo-once，均出现 git_diff 的 policy_denied/stop_task |
| 产物未通过隐藏测试 | 5 | rc-invalid-args、rc-patch-conflict、rc-search-truncated、mm-latest-correction、dl-regression |

不同原因存在重叠，不能把下列影响题数相加，也不能从一次运行推断各因素的因果贡献比例。

## 1. 评测契约不完整：当前最高优先级

`src/tricoder/evals/service.py:687` 向运行时传递 `case.steps` 或 `case.task`；`allowed_changes` 没有在这里转换为模型可见的约束。`src/tricoder/evals/runner.py:332` 却用它判定越界。上述 5 道仅范围失败的题，题面没有禁止新增测试，而 Agent 添加了验证文件。这是题面与评分之间的契约缺口，不能仅依据失败标记认定修复能力不足。

另有 3 道失败题的关键语义没有公开：

| 题目 | 模型可见信息 | 隐藏要求 | 本次表现 |
| --- | --- | --- | --- |
| rc-invalid-args | “修复 normalize”及首次写工具会失败；原函数只有 lower | 还需 strip 首尾空白 | ask_user 请求澄清，目标未改，故障未触发 |
| rc-patch-conflict | “修复 replace_tag”及模拟冲突；原函数只替换一次 | 替换全部匹配 | 重读并编辑，但保留一次替换 |
| dl-regression | “修复 divide 的除零行为” | 除零返回 None | 选择抛 ZeroDivisionError，并为该行为写测试 |

位置：各自 `evals/quality-v1/cases/<id>/case.toml:3` 和 `verifier/test_hidden.py`。divide 的 `float | None` 类型注解提供线索，但不等于完整的除零契约。

建议新建带版本的修订题库：公开函数预期行为和修改范围；隐藏具体输入与断言，不能隐藏业务需求。普通修复题允许约定的测试目录，专门考察范围遵守的题则明确禁止新增文件。公开约束与评分共用一份声明，并校验多轮 steps 的实际消息。明确需求的题不应通过暗示答案之外的隐藏契约惩罚澄清；另外设置有脚本化用户回复的歧义澄清题。保留旧题库和旧成绩，不事后放宽旧评分。

## 2. Agent 的工具可用性处理存在产品缺口

5 题受到 git_diff 停止影响：cf-update-caller、mm-early-constraint、mm-todo-once、mm-latest-correction、rc-search-truncated。

合成工作副本位于主项目仓库内部，Git 向上发现主仓库。`src/tricoder/tools/command.py:594` 拒绝根目录不等于任务工作区的 diff；`src/tricoder/models.py:335` 将 POLICY_DENIED 默认映射为 STOP_TASK。拒绝读取父仓库是正确的边界保护，但仍向模型提供这个当前不可用的辅助工具，并在调用后终止整个修复，造成非必要中断。真实用户选择仓库子目录作为工作区时也可能遇到类似场景。

独立只读探针确认：在保留的 cf-update-caller 工作副本，Git 根确为主项目；调用 git_diff 返回 policy_denied/stop_task，真正 diff 子进程没有启动。证据不是仅依据模型或实施方描述。

建议 Agent 在工具目录中反映当前工作区的 Git 能力：不可用时不暴露，或为固定辅助工具设计明确的“当前不可用”结果。若允许继续，必须限定于已经确认无副作用的能力缺失，不应把全部 POLICY_DENIED 改为可恢复，更不能放行父仓库读取。命令越界、取消、清理失败和未知副作用仍保留原门禁。此项是工具可用性改进，不需要重启搁置的 Replan 功能项目。

评测则显式区分独立仓库、普通目录和位于父仓库下的子工作区。需要 Git 的题应使用合规隔离的仓库夹具；也保留无 Git 和子工作区用例验证降级。不要仅给全部题初始化仓库来掩盖产品缺口。

## 3. 有模型错误和工具调用摩擦，但尚不足以定位通用推理缺陷

- mm-latest-correction 的第二轮已经要求“以 latest 参数为准”，最终实现却用 `if latest` 决定是否回退。独立执行产物确认 `select('red', '') == 'red'`，非空 latest 则正确。这是具体的边界语义错误；本次没有结构化压缩或持久化，不能因此归因于持久记忆丢失。建议题面明确空字符串也是有效值，Agent 的验证习惯覆盖此类假值边界。
- 7 题出现 apply_patch invalid_argument，多题改用 edit_file 后成功。`src/tricoder/tools/write.py:490` 的工具说明仅称受限 unified diff，解析失败在 504 行统一返回泛化消息。`src/tricoder/patches.py:37` 实际区分多种语法错误，但审计未保留可用于归因的补丁正文，因此不能断言是某种补丁格式或解析器 bug。
- rc-patch-conflict、dl-regression 在第 8 轮仍未 finish，探索和调用失败消耗了预算。cf-update-caller 的 unverified 则发生在 git_diff 提前终止后，不能把全部 unverified 都归为轮次不足。

建议先补明确的补丁格式示例，以及不含源码/密钥的稳定错误子类型，验证模型能据此纠正。不要未经证据就兼容全部补丁语法。将剩余轮次和未完成验证义务明确反馈给模型，减少重复探索；是否提高 8 轮预算，用固定题库的预算对照实验决定，并同时报告成本。不能用更宽预算或 compileall 成功替代行为正确性。

## 4. 评测覆盖与诊断能力也需要加强

- `src/tricoder/evals/faults.py:70` 的 provider_transient 只记录“透明重试已经发生”，没有制造传输异常并执行真实适配器重试。独立探针在未提供 Provider/transport 的情况下调用钩子就使计数变为 1。本次该题通过不能证明网络重试实现可靠。
- 输出截断与补丁冲突同样采用固定错误返回；它们可以测收到错误后能否继续，但不能替代真实大输出、实际上下文冲突和参数纠错验证。invalid_args 注入不检查新参数是否真的修正。应区分模拟错误恢复题与真实机制契约测试；使用离线可控 transport/实际冲突夹具验证宿主机制。故障未触发保留 unknown；不能算恢复成功。
- `src/tricoder/evals/scenarios.py:154` 多轮结果用所有 turn 的 ok 做 AND，同时摘要和 verification 取最后一轮。前轮失败不会被后轮修好抹去是合理的，但报告应保留逐轮状态、首个阻断原因和最终产物状态。mm-todo-once 的第二轮正常完成并不能推翻第一轮被终止。若情景要求失败后继续，应在情景契约中明确，而不是为了提分忽略失败轮。
- memory 类当前真实题主要检验短多轮约束，memory off/off，无法测长上下文压缩、持久化重启后的信息保真。需要增加确实触发对应机制的题及触发证据；不要直接把短函数正确性指标命名成长期记忆能力。
- 本轮只有一个模型、每题一次；30 题还包含 11 道 contract，不能宣传为 30 道真实模型质量测量。当前小函数修复也不足以代表复杂仓库中的需求实现、接口迁移及回归处理。先校准现有题，再增加多文件依赖、可见测试驱动的修复和较真实的交付任务；区分 dev 调参与未经调优的 holdout。

## 推荐实施顺序与验证方法

1. 先修订题面、允许路径和评分的共同契约，版本化保存；同时完善逐轮失败与故障触发报告。
2. 再改 Agent 的 Git 能力暴露和固定辅助工具不可用处理，补离线正反例，保留安全边界。补丁错误诊断紧随其后。
3. 用“当前 Agent × 修订题库”建立新基线，再用“修改后 Agent × 同一修订题库”比较，避免同时改两边后无法归因。19 题各一次先筛查；确认有效后每题至少 3 次，按模型分别报告，不能混成一个成绩。
4. 扩展真实机制故障测试、长上下文/持久记忆题和复杂任务。稳定前不以总通过率单指标驱动修改，也不因本次结果启动 Replan 重构。

这些是待实施建议，不是已经验证有效的修复。

## 本次实际验证与边界

已逐题核对保留的审计/产物、题面/隐藏测试、评分与工具实现；新增只读离线探针执行退出 0，复核 19/6/14/11 汇总，复现 git_diff 的父仓库拒绝及三种最终产物行为，并验证 provider_transient 计数钩子的性质。脚本与结果分别在 `runtime/quality-v1-real-20261019/diagnosis/probe.py`、`probe-results.json`，未覆盖旧证据。

本次未新增真实模型调用，未读取密钥或用户数据库，未修改生产代码、正式测试和题库，未安装、提交或推送。未验证建议修改的效果、跨 Provider 稳定性或不同预算的因果影响。原始单次实验条件与全量逐题结果见 `2026-10-19-quality-v1-real-baseline.md`。
