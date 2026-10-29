# R4/P2修复：交给coding session的提示词

下面整段可直接复制给实施会话。

---

你是 TriCoder 本轮 coding 实施者，只修复独立复审新增的 R4/P2。使用中文、单会话执行，不派生子 Agent。请实施并验证，不只给建议；完成后交给独立 reviewer，不自行宣布复审通过。

工作区：`D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli`

规划时分支：`codex/eval-metrics-delivery-guidance`
HEAD：`d6d6c4c8245bdffc19cba03c37b2bfe316f7b448`
本地main：`a81ec2457ece970756b00424393e5f2df9253858`
PR：`https://github.com/PooooLish/Tricoder-Agent/pull/1`

这些值只用于定位，接手先核对实际状态。工作树含上一轮生产/测试实现及多批未提交/未跟踪文档，HEAD不是本次actual before。保留全部修改、旧提交和runtime证据；不reset/clean/stash，不提交、不推送、不合并。

先读：

1. `../../AGENTS.md`、项目 `AGENTS.md`、`README.md`、`pyproject.toml`。
2. `project.md` 和 `docs/reviews/2026-10-19-reviewer-handoff.md` 最新条目。
3. `docs/reviews/2026-10-29-windows-cleanup-p2-independent-review.md`，重点第2节R4。
4. **本轮完整计划**：`docs/superpowers/plans/2026-10-29-r4-observer-lifecycle-fix.md`。
5. 旧三项计划/实施报告，理解仍必须保持的R1/R2/R3、资源所有权、期限与隔离helper契约。

原R1/R2/R3的具体复现条件已关闭，整体验收因R4不通过。不得删除旧回归或将旧绿色结果当本轮验收。

## 要修复的问题

`src/tricoder/process/windows_job.py:126-135` 用 `worker.ident is None` 直接判停止，用 `join/is_alive` 判退出。两个窗口不成立：

- 原生线程已创建，Python启动确认被中断，ident仍为空，但目标稍后会运行。
- Python3.11.6的join/线程状态等待被中断后，目标仍运行而is_alive可能已变False。

当前 `cleanup` 随后发布complete、关闭port/Job。worker迟到完成OpenProcess并登记extras，调用方却已收到清理成功。`control.py`的主异常自动二次清理会触发这一误确认；`_WindowsJob.close` 已关闭分支以后不再回收迟到资源。

独立公开调用链探针已证明：主KeyboardInterrupt保持，但无cleanup_failed、无cleanup_resource、scope无pending；observer未退出而Job=0/port为空/complete为真；observer退出后1个有效精确句柄仍未回收，再次resource.cleanup也没有回收。

## 实施要求

- 以完整计划为准，优先只改windows_job.py及必要control.py接线。补显式启动生命周期、owner、worker完成握手及可靠退出证明。启动结果未知不能当从未启动；done事件不能单独替代OS退出。
- 推荐先验证“worker自身取得不可继承的精确Windows线程句柄、立即登记owner、清理按原剩余期限等待该句柄退出”的机制。计划已给DuplicateHandle官方依据；不把主线程伪句柄或可能复用的TID当observer身份。其他机制必须提供同等证明，不得未经验证全面替换线程框架。
- 停止消息不能依赖失真的is_alive决定是否发送；observer仍可能登记资源时不得complete、关闭port/Job或释放owner。成功必须包含最后加入的句柄，Close失败仍保留exact引用。
- 主中断不被二次中断覆盖；未确认时传播cleanup_failed/cleanup_resource并由TaskCleanup/Eval持久owner持有。下一verifier/case必须阻断；丢弃异常/结果后资源仍有owner，原failed/评分不被清理成功改写。
- 新ready/退出等待有界；清理重试只沿用/收紧原期限，不续发5秒。正常启动不启动任务终止期限，保持原搜索超时→模型等待至少5.2秒→搜索/compileall/finish恢复路径。
- 新ready握手若检查取消，不能被control工厂包围tracker.start的 `except Exception` 吞成普通初始化失败；补本/父令牌的取消回归，保留取消映射、原异常及exact owner。
- 现有Thread.start整体mock抛RuntimeError的fixture不能作为生产“确定未启动”的证据。按计划拆分明确启动前失败与启动结果未知，保留前者关闭断言、补后者pending断言，并说明适配原因。不能识别mock/异常正文走测试专用逻辑。
- 默认不改TaskCleanup、Agent、Session、题库、评分、权限、默认预算；不推进Replan或Skills，不恢复Python3.12。若确需新增生产模块，更新隔离helper复制闭包并验证 `-S` 子进程，避免重复发生模块漏复制。

## RED与真实验收

先建立 `runtime/ec29-r4fix-<唯一后缀>/`，保存actual before/hash（含未跟踪文件），保护旧3505索引、5723保护索引、2720实施安全证据及本轮reviewer索引。不要对metadata-only数据库/缓存读内容或声称hash已验。

独立证据根：`runtime/ec29-review-p2-7c19e4ab/`。复制/适配这些有效探针到新证据/正式测试：

- `test_startup_boundary.py`
- `test_join_boundary_v2.py`
- `test_real_join_boundary.py`
- `test_public_join_boundary.py`

旧 `test_join_boundary.py` 是收尾有误的初版，不作产品RED。保留所有旧日志。每条新命令用独占TRICODER_V3_EVIDENCE，不能原地覆盖。若新实现不再走join，把注入点移到新的真实等待边界，证明注入实际发生；不能以旧注入未触发当通过。

至少覆盖：启动确认中断；join/原生等待中断；done已发布但线程未退出；OpenProcess已取得句柄尚未登记；身份获取/Wait/Close/Post失败；二次中断保留主异常；期限已耗尽；明确启动前失败和正常成功对照；公开run_bounded_process及Eval持久owner门禁。真实native返回值不能伪造成功，测试自有finally清理必须单独记账。修复前RED应是业务契约失败，不能是ImportError/fixture异常/握手超时。

使用现有 `.venv/Scripts/python.exe -X utf8 -B`；不安装依赖。按计划运行新边界、207项原聚焦基数加新增方法、最终冻结源码后的全量2299项原基数加新增方法、compileall。保留R1两个交错点各10轮、R2负例、R3四组真实取消、冷启动、输出超限10轮和搜索5.2秒回归。skip单列不计通过，重复轮次不计额外方法。全量期间如源码/辅助脚本变动，必须重新验证最终字节。

## 交付与限制

- 只用隔离合成数据；不读取.env.local、真实密钥或用户数据库，不调用真实模型。
- 不用sleep、忽略目录删除失败、扩大权限/预算、跳过测试、修改解释器私有状态或禁用中断消除失败。
- 提交工作成果包括：生产/测试修改、actual before/after/delta/hash、逐命令完整日志与退出码、全部中途失败、真实句柄/owner/期限收据、安全证据保护核对、实现自审和未验证范围。
- 新建 `docs/reviews/2026-10-29-r4-observer-lifecycle-implementation.md`，追加project/handoff，保留所有历史正文。解释选择的线程完成证明、启动未知如何处理、如何防止迟到登记，以及旧fixture适配的行为意义。
- 标明本地验证对应的最终候选哈希；当前未获推送授权，不能借旧SHA的CI绿色作本轮证明。历史CI40721403072两次失败的精确根因仍未知。
- 完成后状态写“R4实现完成，待独立复审”；不自行关闭发现，不提交、不推送、不合并。独立复审通过前禁止合并，通过后仍等待明确合并指令。
