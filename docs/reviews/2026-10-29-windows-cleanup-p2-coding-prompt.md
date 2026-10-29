# Windows清理3项P2：Coding session提示词

以下内容可完整复制给coding session。

---

你是TriCoder本轮coding实施session，不是独立reviewer。请使用中文、单会话执行，不派生子Agent。修复已确认的R1/R2/R3，完成后交独立reviewer验收；不要自行宣布关闭复审发现或合并main。

工作区：`D:\MaHong\AGENT_WORKSPACE_V2\projects\tricoder-cli`

分支：`codex/eval-metrics-delivery-guidance`

已审源码基线：`d6d6c4c8245bdffc19cba03c37b2bfe316f7b448`

PR：https://github.com/PooooLish/Tricoder-Agent/pull/1

main最后核对仍为 `a81ec2457ece970756b00424393e5f2df9253858`，未合并。开始先读取实际状态；HEAD不是包含本地未提交文档的actual before。

先读：

1. `../../AGENTS.md`、项目`AGENTS.md`、`README.md`、`pyproject.toml`。
2. `project.md`和`docs/reviews/2026-10-19-reviewer-handoff.md`最新条目。
3. `docs/reviews/2026-10-29-eval-cleanup-independent-review.md`。
4. **主实施文档：`docs/superpowers/plans/2026-10-29-windows-cleanup-p2-fixes.md`。**
5. 前轮实施报告`docs/reviews/2026-10-29-eval-cleanup-implementation.md`，只作背景；不把自测结论当独立验收。

三个问题均在`src/tricoder/process/control.py`，行号基于已审提交：

- **R1/P2，438/449/455行：** QueryInformationJobObject后新增的后代没有进入精确句柄等待集。独立真实5/5轮ProcessResources.finish返回True时，预持有子句柄仍WAIT_TIMEOUT=258。必须证明成员完整性及逐个退出，不能以第二次空列表或计数归零替代。
- **R2/P2，439/447/455行：** captured是每次重置的局部变量。OpenProcess失败、查询失败或4096容量不足后，空列表重试错误返回True并关闭Job。需要跨重试保存不确定状态，只有可靠的新证据才能解除。
- **R3/P2，148/525行：** 取消只在恢复helper前检查。Toolhelp/OpenThread期间取消后仍ResumeThread；真实合成脚本SetEvent证明已执行。透传原CancellationToken，在实际恢复前复检；保留native句柄owner及取消清理语义。

按主文档先留actual before并建立RED，再修R2/R3，然后验证R1机制原型、集成及完整回归。R1优先评估从Job建立时追踪生命周期并做完整性核对；完成端口普通通知不保证送达，不能单独作为完整性证明。也可采用经证明的成员新增阻断方案，但不能凭猜测引入native机制。说明最后一个枚举/事件之后的新成员为何不会漏掉、漏消息/API失败如何保守处理、各句柄和监控资源由谁持有。机制不能证明时明确R1未完成，不刷绿或将所有命令都判cleanup_failed冒充修复。

独立证据目录：`runtime/ec29-review-6e1b2ea2/`。重点读取：

- `test_native_review.py`及三个`capture-*.json`、`native-boundaries.log`。
- `real_late_threaded_member_v3.py`、`real-late-threaded-member-v3.json`、`real-threaded-v3.log`。
- `real_cancel_during_snapshot.py`、`real-cancel-during-snapshot.json`、`real-cancellation.log`。
- `focused.json`、`final-review.json`与完整复审报告。

不要原地重跑旧脚本写入旧目录。新建 `runtime/ec29-p2fix-<唯一后缀>/`，复制或改造成新增正式测试；每轮测试使用新的TRICODER_V3_EVIDENCE。适配接口/门闩时保留业务断言，不把新API未mock、握手超时、ImportError当成产品RED或GREEN。旧第一版`real-threaded-enumeration`来自测试清理断言错误，不是有效完整产品RED。

验收至少包括：R1真实受控交错10轮、原输出超限真实进程树10轮及立即目录删除、冷启动；R2三个失败后重试负例与部分句柄/Wait/Close失败；R3快照及OpenThread期间取消、父令牌取消及真实脚本事件负例；原187项聚焦相关模块及新增测试；最终冻结字节上的完整unittest（原2279项基数加新增）与compileall。原搜索超时后模型等待至少5.2秒，再搜索/compileall/finish，以及取消、已有期限、未确认清理、Session owner负例必须保留。skip单列，不能计为通过。

上层Eval cleanup_resource引用交接已经修复，不能倒退。失败验证不能因后续清理成功改判通过；failed不能重置，pending只有实际确认后移除；未确认资源继续阻断下一verifier/下一case executor；原终止期限不得延长；主中断不能被二次清理中断替换；资源不能序列化。诊断仅受控类别、errno/winerror和必要状态，不输出argv、环境、源码、输出、异常正文、绝对路径或秘密数据。

保留所有原提交及本地未提交文档；保全runtime/mg28、runtime/ec29-afafcaf4、runtime/ec29-review-6e1b2ea2和规划记录，旧evals766份+mg28安全索引2739份共3505份不得改变。对实际修改范围保存before/after、包含新增文件的delta/hash、命令、数量/失败/error/skip/退出码、全部中间失败、最终源码哈希和保护文件核对。更新project/handoff但保留旧正文。

不读取.env.local、真实凭据或用户数据库；只用隔离合成数据。不安装本机依赖、不调用真实模型、不推进Replan、不放宽默认权限/预算/评分、不修改题库、不恢复Python3.12。不reset/clean/stash；不提交、不推送、不合并。需要最终候选的远端Windows3.11 CI时先报告本地结果，待用户另行授权推送；没有新CI就明确标记尚未执行，不能借用旧SHA的绿色CI。

最后按R1/R2/R3逐项报告：具体实现、为何满足完整性/退出/取消要求、RED→GREEN、真实证据、完整测试结果、保全核对、剩余风险和未验证范围。不能把这些修复追溯认定为历史CI 40721403072两次失败的精确根因。交独立reviewer复审，等待后续明确合并指令。
