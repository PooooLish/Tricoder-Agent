# 可恢复搜索超时修复：独立复审

日期：2026-10-19。结论：**本次范围内通过，原 P2 关闭；未发现需要修复的新问题。**

## 审查边界

- 独立 reviewer 审查 `docs/superpowers/plans/2026-10-15-recoverable-search-cleanup.md` 的已完成实现；未修改生产代码或正式测试。
- 已读取工作区/项目 AGENTS.md、README、pyproject、project.md 最新条目、reviewer 交接文档及实施方案。
- HEAD 为 `806b7427760f57abf5571081959b17d30a6be4ab`，工作树包含多批修改；未把 HEAD 当成本次前置基线。
- 以 `runtime/recoverable-search-cleanup-20261015/before/` 重新生成当前差异，并核对实施方 `implementation.diff`。生产增量仍为 control.py 的恢复参数/判定及清理后取消检查、search.py 的唯一启用点；新增未跟踪集成测试已单独阅读并执行。
- 本次 `src/`、`tests/` 下全部 225 个 Python 文件的前后 SHA-256 一致。保留全部现有修改及旧日志。

## 原 P2 的独立对照

reviewer 自编 `independent_probe.py` 在同一个 TaskCleanup 和 ToolRegistry 内运行真实 regex helper，等待超过 5 秒后运行真实 compileall。before 对照仅在探针进程中载入两份生产 before 快照并替换调用绑定，未覆盖工作树；固定 helper 与缓存实现保持当前版本。

| 观察 | before 快照 | 当前实现 |
| --- | --- | --- |
| 搜索反馈 | TIMEOUT / REPLAN | TIMEOUT / REPLAN |
| 搜索结束时 failed / pending | false / false | false / false |
| 搜索结束时共享 deadline | 已启动 | None |
| 下一轮等待 | 5.313 秒 | 5.312 秒 |
| compileall 退出码 | 0 | 0 |
| 工具最终结果 | cleanup_failed | 成功 |
| 编译后 failed / pending | true / true | false / false |

当前实现另确认无工作区 pyc、无存活 helper/读写线程、独占编译缓存已删除。before 的残留由探针记录的 exact owner 在观察完成后回收，未清空失败事实。

仓库的真实 CodingAgent 集成用例也由本 reviewer 重新运行：同一 Agent/scope 创建文件 → 灾难性 regex 超时 → Provider 等待至少 5.2 秒 → 再次 regex → compileall → finish，完成五轮、最终验证通过且无待清理资源。其对应清理未确认负例阻止后续命令和 finish，并阻止复用。

## 重点代码核查

- `src/tricoder/process/control.py:103`、`:182`：参数默认 False；普通命令和 Eval 调用未启用恢复。
- `src/tricoder/process/control.py:259`：只有显式恢复的 timeout 不再触发 terminal；取消和输出超限仍触发。未知执行异常仍走原终止清理分支。
- `src/tricoder/process/control.py:77`：复用局部清理预算；已有任务期限通过 begin_termination 继承，局部失败在 `:86` 沿用已消费截止时间、mark_failed 并 retain exact resource。没有重置 deadline、failed、pending 或更换 scope。
- `src/tricoder/process/control.py:264`：清理完成后重新检查取消，沿用 resources.deadline；异常携带同一资源，不进入重复 finish。独立实进程探针覆盖默认普通命令正常退出后收到取消、可恢复 timeout 清理中取消，以及取消同时清理确认失败。
- `src/tricoder/tools/search.py:149`：唯一生产启用点为固定 `-I -S` regex helper，只传文本；未暴露到模型 schema。`:156` 优先检查清理失败，再在 `:158` 映射 TIMEOUT/REPLAN；搜索超时不签发验证证据。
- `src/tricoder/task_cleanup.py:35`、`:92`、`:131`：已有期限只收紧；worker lease/旧 scope 所有权、阻止复用及 exact resource 重试机制未被绕过。已有/过期期限探针均未延长期限，过期清理未确认仍保留失败所有权。
- `src/tricoder/process/cache.py:77`、`:92`：缓存继续继承终止期限并等待进程依赖，未放宽身份检查或清理成功门禁。既有替换目录、身份失败及阻塞删除负例通过。
- `src/tricoder/tools/command.py:391`：普通命令未传入恢复选项；超时/执行不确定与副作用 UNKNOWN 的停止语义保持。补充 Runtime 用例验证清理失败阻止资源复用、并发任务/配置锁以及取消令牌发布边界。

## 本次实际验证

Windows / Python 3.11.6，全部使用现有 `.venv/Scripts/python.exe`，未安装依赖。

| 测试组 | 数量 | 结果 |
| --- | ---: | --- |
| test_subprocess_control | 15 | OK |
| test_search_limits | 12 | OK |
| test_search_timeout_recovery | 2 | OK |
| test_command_cache | 9 | OK |
| test_agent_async | 17 | OK |
| test_cancellation | 7 | OK |
| test_provider_liveness | 13 | OK |
| test_runtime_coverage | 2 | OK |
| test_finish_verification_recovery | 25 | OK |
| test_eval_regression_suite | 8 | OK |
| 选定 SessionRuntime 所有权/锁/取消用例 | 4 | OK |
| 合计 | **114** | **0 failures/errors/skipped** |

- reviewer 自编独立探针：8 个场景符合预期，包括 before 原问题复现、当前恢复、3 个清理取消组合、2 个已有期限、启用恢复时的输出超限终止；退出 0。before 失败是预期对照，不计为当前产品失败。
- `python -B -m tricoder --help`：退出 0；`git diff --check`：退出 0。
- 原始日志保留 1 条 asyncio 慢回调诊断（0.156 秒）；不隐藏、不计为失败。
- 日志汇总首次按 UTF-8 读取 Windows 子进程输出时遇到 CP936 解码错误，随后按实际编码读取完成；这是 reviewer 日志读取问题，不是产品测试失败，原始日志未改写。
- 早期只读定位命令曾指向不存在的 registry.py/service.py 和 PowerShell 通配路径；已用 `rg` 定位真实 `tools/__init__.py` 并完成阅读，不作为产品证据。

证据目录：`runtime/recoverable-search-cleanup-20261015/review-20261019/`。精确测试命令与退出码见 `test-commands.json`、`additional-commands.json`；测试汇总见 `test-summary.json`；对照原始数据见 `independent-probe.json` / `.log`，可复运行脚本为 `independent_probe.py`。差异及完整性记录为 `current-vs-before.diff`、`source-hashes-before.json`、`source-integrity.json`。

## 未验证范围与后续

未重跑完整项目、其他 Python 版本、Linux/macOS、真实模型/外部服务或分发产物；114 项与 8 个探针不等于整个项目无缺陷，也未验证任意并发调度交错。实施方的 1744 项全量记录未计入本次验收数字。

没有读取真实凭据、.env.local 或用户数据库；Runtime 测试使用临时合成 SQLite。没有依赖安装、真实模型调用、提交或推送，也没有自动推进历史待办。当前复审无阻塞，下一步等待用户指定后续审查范围。
