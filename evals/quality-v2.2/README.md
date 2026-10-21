# Quality v2.2：公开交付契约

从实施前 quality-v2（benchmark quality-v2.1）的实际内容复制，来源见 source-v2.1-sha256.json。仅 19 道质量题增加非交互说明，3 道 memory 首轮明确只确认；工作区、隐藏 verifier 和 11 道 contract 原样保留。旧版本与真实成绩不回写。本版本尚未运行真实模型。

主入口：`tricoder eval --experiment evals/quality-v2.2/experiment-quality-baseline.toml --dry-run --no-color`，19 道质量题，各一次。

预算实验只准备、不自动执行：

```powershell
python -B scripts/prepare_delivery_budget_experiment.py --suite evals/quality-v2.2 --output runtime/<全新目录>
tricoder eval --experiment runtime/<全新目录>/budget8/experiment.toml --dry-run --no-color
tricoder eval --experiment runtime/<全新目录>/budget16/experiment.toml --dry-run --no-color
```

每组均 19 quality、一次、seed23、DeepSeek deepseek-v4-flash、memory off/off、3600 秒调度预算；只将 16 组的 19 个 max_rounds 改为 16，11 contract 字节不变。共享 code/ 快照与 source-sha256.json，allowed-delta.json 可逐文件校验；未来真实执行前须确认源码仍等于冻结快照。严格比较器拒绝跨预算比较是预期，后续应单独报告预算设计变量，不能宣称这两组只有 Agent 改进或统计显著。产品默认轮数不变，本轮真实调用为零。

以下为来源版本的历史说明（内容保留作背景）：

# TriCoder Quality V2

来源为 quality-v1 的 30 道题：19 quality、11 contract。ID、split、每 user_turn 8 轮预算、workspace 与 hidden verifier 原文保持；原 v1 和历史成绩不改。source-v1-sha256.json 固定复制来源，验收逐文件比较。

19 道质量题通过 expose_edit_scope=true 将 allowed_changes 同源注入每个 user_turn；原生产文件范围保持，追加 tests 和 tests/*.py 模式。可添加测试但不强制；根目录测试文件、tests 下嵌套文件和保留 verifier 路径不允许。11 道契约题的 case.toml 和脚本保持逐字一致。

当前 benchmark_version 为 **quality-v2.1**，目录与 suite id 仍为 quality-v2。仅 19 道质量题显式选择 path_match_mode="workspace_root"：allowed_changes 和 required_changes 均从工作区根按路径段匹配，同段数、大小写敏感；每段支持 `*`、`?`、字符组，通配符不跨目录，不支持 `**`，拒绝非法或非规范相对 POSIX 路径。未指定模式默认 legacy，保留 PurePosixPath.match 的原后缀语义。旧冻结副本仍是 quality-v2，其分数不回写；比较器拒绝跨 benchmark_version 严格配对。

评分的 capture_snapshot/changed_paths 当前只记录普通文件，本轮修复的是文件路径范围判定，不包含空目录或完整目录级审计。模式 tests 的字符串匹配不代表目录变化已被快照覆盖。

模型只看到公开业务需求与允许路径，不看到隐藏输入、断言、测试源码或标准答案。mm-latest-correction 的修正只进入第二轮，第一轮仍是原决定。

## 逐题修订

| 题目 | 相对 v1 的修订 |
| --- | --- |
| cf-backward-api | 原业务语义保持；公开同源范围并允许约定 tests 路径 |
| cf-module-interface | 公开业务语义；公开同源范围并允许约定 tests 路径 |
| cf-parameter-validation | 原业务语义保持；公开同源范围并允许约定 tests 路径 |
| cf-price-discount | 公开业务语义；公开同源范围并允许约定 tests 路径 |
| cf-update-caller | 原业务语义保持；公开同源范围并允许约定 tests 路径 |
| dl-regression | 公开业务语义；公开同源范围并允许约定 tests 路径 |
| mm-early-constraint | 原业务语义保持；公开同源范围并允许约定 tests 路径 |
| mm-latest-correction | 公开业务语义；公开同源范围并允许约定 tests 路径 |
| mm-todo-once | 公开业务语义；公开同源范围并允许约定 tests 路径 |
| rc-invalid-args | 公开业务语义；公开同源范围并允许约定 tests 路径 |
| rc-patch-conflict | 公开业务语义；公开同源范围并允许约定 tests 路径 |
| rc-provider-transient | 公开业务语义；公开同源范围并允许约定 tests 路径 |
| rc-read-once | 公开业务语义；公开同源范围并允许约定 tests 路径 |
| rc-search-truncated | 原业务语义保持；公开同源范围并允许约定 tests 路径 |
| sf-empty-input | 原业务语义保持；公开同源范围并允许约定 tests 路径 |
| sf-negative-boundary | 公开业务语义；公开同源范围并允许约定 tests 路径 |
| sf-subtract | 原业务语义保持；公开同源范围并允许约定 tests 路径 |
| sf-tail-boundary | 公开业务语义；公开同源范围并允许约定 tests 路径 |
| sf-unique-stable | 原业务语义保持；公开同源范围并允许约定 tests 路径 |

补足四项已知语义：normalize 首尾空白/小写/内部空白及空输入；replace_tag 全部替换/无匹配；divide 除零 None/正常 a/b；latest 包括空字符串。逐项检查其他 verifier 后，补充百分比折扣及零折扣、双参数姓名空格/title、整数文本空白/负号、increment 增一、tail 正常行为、正数保持、待办稳定顺序，均只公开业务契约而非隐藏输入或实现代码。其余已有需求与工作区接口能解释断言，保持语义。

## 测量边界

provider_transient 是 counter_only 模拟计数，其余已触发工具故障是 synthetic_tool_result；测试收到模拟错误后继续，不代表真实 HTTP 重试、参数修复或截断机制被触发。retries 包含模拟恢复计数。未触发时 recovered 为 unknown。memory off/off 只能测短多轮上下文，不能证明持久记忆或压缩质量。

历史 A/B 使用同一冻结的 quality-v2（不是本轮 quality-v2.1），DeepSeek deepseek-v4-flash，每题一次，seed=23，quality only，memory off/off，原规划设置，每 user_turn 8 轮，max_trials=19，3600 秒调度预算。只比较冻结 v2 的 A/B，不将 v1→v2 题面改善视为 Agent 提升；单次结果不宣称统计显著。本轮仅离线修复评分与报告，没有重跑真实 A/B。
