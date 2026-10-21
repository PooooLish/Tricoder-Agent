# Quality V3 评分器修复独立复审（2026-10-21）

本轮约定范围通过，无新增阻塞发现。Q3-R1/P2、Q3-R2/P2、Q3-R3/P2关闭。此结论基于当前代码、actual before及本轮独立验证；实施方记录仅用来定位范围。

| 发现 | 当前代码位置 | 独立结论 |
| --- | --- | --- |
| Q3-R1 | `evals/quality-v3/cases/sf-aware-time-order/verifier/test_hidden.py:9`、`:31` | 调用前复制完整expected及输入；完整字段、嵌套值、无id、稳定排序和输入不变均检查。原id-only及新嵌套破坏被拒，完整原记录/副本接受。关闭。 |
| Q3-R2 | `evals/quality-v3/cases/cf-cli-result-contract/verifier/test_hidden.py:12`、`:25` | CLI与main各测3/4参数；每次独立捕获输出并检查返回2/空stdout/非空stderr。入口不同步及只首次诊断被拒，正常与整数枚举接受。关闭。 |
| Q3-R3 | `evals/quality-v3/cases/cf-cache-copy-isolation/verifier/test_hidden.py:25`、`cf-package-api-migration/verifier/test_hidden.py:20` | 保留真实目标调用，去除强制一次和原样参数限制；多次/slots/关键字/预处理接受，绕过get/新导出拒绝；包导入绑定finally恢复。关闭。 |

实际before清单907项、after909项，8项修改+2新增，均与交付一致。物理before保存8份实际修改文件，after10份；其余未变文件按清单与当前内容核对，并非宣称907份完整副本。没有生产src、case/workspace、实验清单、权限、预算或AND变化。独立delta见证据目录。815份源码/测试/脚本/题库文件及11,088份受保护历史文件hash保持。

本轮独立运行完整unittest discovery：**2001项，1986通过、15跳过、0失败/错误，837.646秒，exit0**。包括335项Eval（333通过、2skip）、163项V3，以及原125变体（35接受/90拒绝）和新增26变体（15接受/11拒绝）；这些是全量的子集，不重复计数，也没有单独重跑三组来增加数字。

原reviewer8例在actual before与当前各运行一次，共16次真实隔离评分：before重现两错误误过、两合法误拒；当前2错误拒绝、6合法接受。追加6次独立评分（3错误拒绝、3合法接受），覆盖嵌套值损坏、输入突变、CLI后续错误无诊断、UTC完整副本、类方法keyword及包namespace预处理。均实际运行公开/隐藏子进程和评分；Agent完成声明为合成，不当作真实Agent成绩。

四次CLI dry-run均exit0，30/90/22/8 trials。git diff --check exit0。原搜索P2、取消/清理失败/已有期限/普通命令及owner相关回归包含在本次全量中，历史关闭结论维持。

新证据：`runtime/quality-v3-review-fixes-20261020/review-20261021/`。入口为`verification.json`、`full-tests.log`、`full-tests-completion.json`、`initial-verification.json`、`independent-delta.patch`、`replay-before/`、`replay-current/`、`extra-probes.json`；完整skip清单在verification.json。首次索引脚本误把hash清单当作全量副本，遇缺失before/AGENTS.md退出1；改为核对8/10实际副本与其余未变当前hash后通过，不是产品测试失败。

限制：仅当前Windows/Python环境；15项平台/符号链接权限skip未验证。有限变体不构成任意程序数据流证明或目录级审计；本报告不宣称真实模型质量提升，不与v2.2跨benchmark归因。没有修改生产/题库/测试、安装依赖、读取凭据正文/用户数据库或提交推送。

复审通过条件已满足，下一步继续此前授权的DeepSeek deepseek-v4-flash、quality-v3 30题各1次、16轮、memory off/off、seed23真实复测。真实结果另行记录，不能用离线通过代替。
