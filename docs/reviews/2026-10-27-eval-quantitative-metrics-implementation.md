# Eval 量化指标实施报告（2026-10-27）

**实现完成，待独立复审。** 本文是 coding 实施和自测记录，不替代 reviewer 独立验收。
单会话按 Task1→5 离线实施，未调用真实模型、读取真实凭据/用户数据库、安装依赖、提交或推送。
未修改题库、Agent/Provider/Engine/Session/process、默认权限/预算、finish、AND 或资源所有权。

证据根目录 `E = runtime/eval-metrics-20261027/implementation-1cefa5ed1d/`。
原始 planning 文档及项目/交接已有未提交修改保留，HEAD 不是本轮 before。

## 实际增量与要求映射

| Task / 要求 | 文件与入口 | 实施及验证 |
| --- | --- | --- |
| 1：组合估计与样本分母 | src/tricoder/evals/quantification.py；models.py:TrialTimings/TrialPlanEntry；test_eval_quantification.py | 整题 sample、condition/case/repetition 唯一身份、quality-only、逐题宏平均、n<k/缺计划/未跑/未知严格不可用；手算 n=3,c=2、不等 n、error 保守失败、四维与 status 区分 |
| 1：token/资源统计 | quantification.py:summarize_counts/_efficiency | main/summary 分开，字段级 unknown、nearest-rank、缓存不双计、失败摊销、未知交付分母阻塞；provider_reported/billing unknown，不改货币字段 |
| 2：计划和可选字段 | models.py、experiment.py、service.py、report.py；test_eval_quantification_report.py | freeze 全计划及 kind/category/split，包括 error/not_run；未知默认可补可信计划，明确冲突拒绝；split/timings 往返和旧默认；两条件×两题×三次=12行隔离 |
| 2：新旧报告 | report.py:experiment_report_as_dict/render_quantification_markdown | schema2/旧指标/指纹键不变，新增 quantification，表格明确不可用原因；legacy stable 和 duration 不冒充新可靠性/阶段时间 |
| 3：阶段计时 | runner.py:_TimingObservation/_run_case；service.py:_trial_from_case_result；test_eval_timing.py | Eval 层单调时钟；100=30+20+50，旧 duration=80；最后清理计入 total，零阶段、准备失败、executor异常、隐藏超时、安装/清理失败、KeyboardInterrupt/async cancellation 原传播保持 |
| 4：离线入口 | analysis.py:analyze_report_payload/run_analyze_command；cli.py；test_eval_analysis.py | 只读显式 result/plan，限大小/数量/类型/reparse；不加载配置/模型/数据库/当前suite；嵌入与显式计划交叉核对，旧缺 split unknown、缺时间 null；独占原子输出与来源 SHA256 |
| 4：严格比较 | compare.py:_quantification_comparison | 原严格指纹/单条件边界保留；按 case/repetition 完全配对且元数据一致才给新差值；条件名可不同；缺计划/不配对差值 null；拒信预计算分数，非法缓存阻塞 token 差 |
| 5：文档与交接 | README.md、docs/eval-quantitative-metrics.md、本报告、project.md、reviewer-handoff | 数据字典、CLI 示例、不可用原因、限制、actual delta 与新鲜验证记录 |

`metrics.py` 无改动，原 aggregate_trial_metrics 回归继续通过。模块不读取时间/环境/文件的纯统计接口与离线 I/O 分离。
所有任务进度、兼容性判断在 `E/progress.md`。缺失历史题类型时不猜其 kind；保留 unresolved_plan_identities，
受影响 condition 的 quality 分母 null，完整条件继续可用。此为第6节旧计划兼容的保守实现。

## RED / GREEN 与中间失败

- Task1：`task1-red.log` 16项全部因纯统计接口缺失失败，exit1；`task1-green` 16通过。
- Task2：16项中6失败，exit1；补计划/序列化/报告后与旧 experiment/report/metrics 共31项通过。
  RED文件误导入基类产生5项重复 discovery，后改为模块导入；继承的5项真实运行回归仍保留，未删除断言。
- Task3：7项中5项因缺阶段观测失败；另2项旧构造器/中断传播本已通过，不虚构RED。
  实施后计时+runner/service 44项通过；最终计时扩展为9项。
- Task4：初始13项失败。第一次GREEN尝试51项中1失败：旧 usage 读取器会把非法数值变unknown，
  因此只在新离线入口增加原始数值校验，保留旧读取器行为；随后51通过。
- 追加边界：17项中3失败+1 argparse SystemExit（历史缺题跨条件污染、比较Markdown缺区块、非法缓存差值、
  非数字k固定错误）。最小修复后连旧compare/CLI共55通过。
- 最终专项首次59项出现1个测试编辑错误：新增方法插入点使原缓存断言落入另一方法、局部变量未定义。
  修正测试位置后59通过；该错误不是生产回归，不计为产品有效RED。
- 额外缓存覆盖/summary三条件/已有False/全缺结果/元数据冲突等断言加入时现有实现已通过，如实视为覆盖补充。

- 全量首轮2060项，1个断言失败+2个cleanup错误，15skip，exit1，493.116秒（墙钟500.672秒）。
  全部集中在未修改的 `test_i2_runtime_scan_incomplete_or_exception_fails_closed`：exception子用例尚未注入
  扫描故障时，前置 `runtime.run_task("本地检查").ok` 为False；随后Windows临时Session/workspace锁文件
  删除报WinError32/267。原日志没有该次RunResult/stop_reason，无法精确归因，也不声称已证明与本包无关。
  既有Runtime/验证门禁/测试未改。独立新进程定向1项通过；整个verification_evidence模块80项=79通过+1skip，
  41.920秒、exit0。保留首次完整失败日志，代码不变再跑全量，结果单列；不把重跑成功当作解释了首轮失败。

全部中间失败完整日志保留，不用最后通过覆盖之前失败。详细命令和耗时见 `validation-ledger.json`、各同名 `.json/.log`。
Task1初次RED使用PowerShell重定向，保留unittest 0.006秒，未单独测进程墙钟；不补造该耗时。

## 本轮实际最终验证

| 日志标签 | 总测试数 | failures / errors | skips | unittest秒 / 墙钟秒 | exit |
| --- | ---: | --- | ---: | --- | ---: |
| quantification-final | 33 | 0 / 0 | 0 | 1.165 / 1.485 | 0 |
| timing-final | 9 | 0 / 0 | 0 | 0.405 / 0.859 | 0 |
| analysis-final | 17 | 0 / 0 | 0 | 0.326 / 0.657 | 0 |
| eval-regression | 394 | 0 / 0 | 2 | 171.241 / 172.391 | 0 |
| full-regression | 2060 | 1 / 2 | 15 | 493.116 / 500.672 | 1 |
| full-failure-targeted | 1 | 0 / 0 | 0 | 1.078 / 1.438 | 0 |
| full-failure-module | 80 | 0 / 0 | 1 | 41.920 / 42.813 | 0 |
| full-regression-recheck | 2060 | 0 / 0 | 15 | 483.069 / 488.016 | 0 |
| dry-baseline | — | 0 / 0 | 0 | — / 1.469 | 0 |
| dry-repeat3 | — | 0 / 0 | 0 | — / 1.438 | 0 |
| dry-dev | — | 0 / 0 | 0 | — / 1.422 | 0 |
| dry-holdout | — | 0 / 0 | 0 | — / 1.391 | 0 |

以上新专项是全部Eval/全量的子集，不把多次运行相加宣称独立测试数；继承的5项experiment回归明确计入当前discovery。
平台/权限skip单列，不算通过。完整名单见 `E/full-skips.json`。

```powershell
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p "test_eval_quantification*.py" -v
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p test_eval_timing.py -v
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p test_eval_analysis.py -v
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p "test_eval*.py" -v
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -v
.venv/Scripts/python.exe -X utf8 -B -m tricoder eval-analyze --result runtime/evals/20261021t062203.210557z/result.json --plan runtime/evals/20261021t062203.210557z/plan.json --ks 1 2 3 --no-color
.venv/Scripts/python.exe -X utf8 -B -m tricoder eval --experiment evals/quality-v3/experiment-quality-baseline.toml --dry-run --no-color
.venv/Scripts/python.exe -X utf8 -B -m tricoder eval --experiment evals/quality-v3/experiment-quality-repeat3.toml --dry-run --no-color
.venv/Scripts/python.exe -X utf8 -B -m tricoder eval --experiment evals/quality-v3/experiment-quality-dev.toml --dry-run --no-color
.venv/Scripts/python.exe -X utf8 -B -m tricoder eval --experiment evals/quality-v3/experiment-quality-holdout.toml --dry-run --no-color
git diff --check
```

`run.py` 为本轮日志封装，实际子进程命令如上；设置 `PYTHONPATH=src;tests`、`PYTHONDONTWRITEBYTECODE=1`。
每个标签使用独占 `TRICODER_V3_EVIDENCE=E/<label>-receipts`。没有执行真实eval命令。
AST解析与 `git diff --check` 通过；没有静默整理已有历史行尾格式。

## 离线数值与历史事实

- 原生来源 `runtime/evals/20261021t062203.210557z/result.json` + 同目录plan.json，只读；
  派生 `runtime/evals/analyze-4ca51c63939d4b68919b9c41a7cd06b8/analysis.json`、analysis.md。
- 30 quality：产物29/30、完整27/30；@1分别29/30和27/30；@2/@3/^2/^3均null/insufficient_repetitions。
- main input1,343,808、output70,940、total1,414,748；均值47,158.2667；失败计入后每次完整成功摊销52,398.0741。
- legacy duration mean24,140.4ms、median22,380.5ms、P95=38,253ms；阶段时间仍null，split全部unknown。
- 来源SHA256：result `985f6b25a91c94a3e76b46f894f0c0a166aee1e2a200afc34628a3446bda75ce`；
  plan `acae9ef3aa7528080811f2b4e2e63f94abf0085389066f9c3feeeb05cec393b6`。分析前后相同。
- 90行合成：`E/synthetic-90-result.json`、`synthetic-90-analysis.json`，30题每题3次、每题2成功，
  @1=2/3、@2=@3=1、^2=1/3、^3=0。它不是90次真实模型调用，也不把旧30次扩成90次。
- 四dry-run baseline/repeat3/dev/holdout = 30/90/22/8，全部exit0；真实清单和题面完全未动。

## 保全、自审与可复核入口

- `before/` + `before-hashes.json`：916份实际源码/测试/题库/文档/脚本及明确根文件副本；
  排除配置秘密、数据库、Git元数据和缓存，不宣称全磁盘快照。`git-status-before.txt`保留既有2修改+2未跟踪规划文件。
- `after/` + `after-hashes.json`、`delta.patch`、`delta-index.json`：全文件索引、实际before→after差异，含新未跟踪文件。
- `tested-code-freeze.json` 固定被测259份src/tests内容；全量结束后核对未变，未用旧测试替本轮证据。
- `historical-hashes.json`与`historical-preservation.json`：19,739份已有结果/日志/冻结证据hash未变，无缺失；
  不复制历史对话/源码日志正文。561份旧/新题库资产均保留；所有源fixture、verifier、清单和split不变。
- `evidence-hashes.json`/`seal.json` 索引本轮日志、收据、快照与派生分析hash；没有覆盖reviewer目录。
- 本轮自审覆盖算式、三值AND/error、condition/plan/metadata、raw输入限制和脱敏、资源计时finally、
  新旧报告、严格指纹/配对、实际增量白名单与历史文件保全；独立reviewer仍需复核。

## 限制与下一步

首轮全量出现未稳定复现的Runtime前置检查/锁清理失败，根因未确认，交reviewer继续关注。
仅当前Windows/Python环境；平台权限skip未验证，其他平台/模型、真实重复稳定性和货币费用均未验证。
Provider reported usage可能遗漏失败请求，本包不能据字段齐全宣称账单完整；缓存和摘要仍保留独立计量。
旧schema1不具备四维/重复身份；缺计划类型的旧题分母无法重建，只能不可用。
文件级工作区快照不扩展到目录级审计。未修CSV或真实任务交付遗留问题，不推进Replan。
原Q3-R1/R2/R3与旧搜索P2的关闭历史保留；本次实现记录不自行新增独立验收结论。
停止并交独立reviewer；90次真实重复实验必须另获用户授权，本会话不会继续付费跑分。

最终全量同代码复跑通过，首轮非稳定失败及上述限制仍保留。精确改动清单见 E/delta-index.json；不将重跑通过当作首轮根因已修复。
证据整理脚本首次生成时发生嵌套引号SyntaxError、exit1；未写入项目文件，修正脚本后继续，不计为产品RED。
