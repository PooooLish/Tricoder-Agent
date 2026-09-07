# Hcode 能力迁移映射

记录日期：2026-09-03

Hcode 基线：`d77927f7f2079b103c8077d321f0b24077c75800`（`main`）

迁移策略：以 TriCoder 为主体进行原生适配；Hcode 仅作为只读设计、算法和测试场景来源。

## 复用与许可边界

Hcode 根目录 `LICENSE` 为 MIT License，版权标识为
`Copyright (c) 2026 PooooLish`。本次对受版本控制的 `hcode/`、`tests/`、
`docs/`、`pyproject.toml` 和根许可证进行了文本检查，未发现额外 SPDX 标识、
第三方源码归属、vendored 代码声明或第二份许可证。TriCoder 当前没有根
`LICENSE`，`pyproject.toml` 也没有许可证字段。

用户已于 2026-09-03 明确选择 MIT 作为 TriCoder 的发布许可证。`reference`
表示只参考行为、接口和测试场景；`adapt` 表示允许在保留 MIT 版权与许可文本、
记录具体来源文件的前提下进行实质性适配；`rewrite` 表示必须围绕 TriCoder 的
安全边界独立重写。Phase 0 尚未复制或实质性改编任何 Hcode 实现。

所有 Hcode 测试都只能作为场景来源，必须改写为 TriCoder 契约测试，不能原样
复制。所有新能力必须经过 `ToolRegistry`、`WorkspacePolicy`、审批、审计、
`Subprocess Control` 和 `ChangeJournal`；权限策略不是 OS 沙箱。

## 来源、落点与复用决策

| Capability | Hcode 精确来源文件 | TriCoder 当前对应实现 | TriCoder 计划目标文件 | reuse mode 与原因 | Phase / 当前状态 |
| --- | --- | --- | --- | --- | --- |
| 流式 Provider | `hcode/client.py`；`hcode/serialization.py` | `OpenAICompatibleProvider.stream()` 已通过 urllib 增量读取 SSE，归一化文本、思考、工具、usage 与完成事件；同步 `complete()` 保持兼容 | `src/tricoder/providers.py`；`src/tricoder/models.py`；`src/tricoder/core/events.py` | `rewrite`：参考 SSE/事件归一化，但按 TriCoder Transport、错误模型和响应上限重写 | Phase 2 / 已完成 |
| 类型化 Agent 事件 | `hcode/agent.py`；`hcode/app.py`；`hcode/tui.py` | `src/tricoder/core/events.py` 定义 Provider/Agent 事件；异步 Agent、批量 CLI 和 TUI 已接入，旧 Observer 保持兼容 | `src/tricoder/core/events.py`；`src/tricoder/agent.py`；`src/tricoder/tui.py` | `adapt`：适配事件分类和载荷模型，但未复制 Hcode Agent 执行循环 | Phase 1–2 / 已完成 |
| 异步运行与取消 | `hcode/agent.py`；`hcode/client.py`；`hcode/app.py`；`hcode/agents/task_manager.py` | `run_with_context_async()` 为规范实现；同步入口包装事件循环；取消贯穿 Provider、重试等待、工具间隙、受管命令、SessionRuntime 与 TUI | `src/tricoder/core/cancellation.py`；`src/tricoder/agent.py`；`src/tricoder/session_runtime.py`；`src/tricoder/cli.py`；`src/tricoder/tui.py` | `rewrite`：取消、锁、审批快照和任务收尾保持 TriCoder Runtime 原子语义 | Phase 1–2 / 已完成 |
| token 上下文预算 | `hcode/conversation.py`；`hcode/context/manager.py`；`hcode/client.py` | `ContextManager` 已使用 Provider input/output usage 锚点；缺少 input 或锚点失配时按 UTF-8 字节保守估算，旧字符硬上限继续生效 | `src/tricoder/core/budgets.py`；`src/tricoder/context/manager.py`；`src/tricoder/agent.py` | `adapt`：仅适配 usage anchor 与估算思想，消息模型和预算选择按 TriCoder 重写 | Phase 1、3 / 已完成 |
| 上下文压缩 | `hcode/context/manager.py`；`hcode/conversation.py`；`hcode/conversation_pairing.py`；`hcode/agent.py` | `ContextManager.prepare()` 按完整任务块和可变长度工具回合归一化；旧 `compact_*` 保留为字符兼容代理，不生成语义摘要 | `src/tricoder/context/manager.py`；`src/tricoder/agent.py` | `adapt`：参考用量锚点；回合分组、固定边界和兼容逻辑为 TriCoder 实现 | Phase 3 / 已完成 |
| 大工具结果落盘 | `hcode/context/manager.py`；`hcode/agent.py` | `ToolResultSpillStore` 使用状态根、Session 哈希目录、随机引用、不覆盖原子发布、容量上限和分段回读；ToolRegistry 返回有界预览，Runtime 负责启动与 `/clear` 清理 | `src/tricoder/context/spill.py`；`src/tricoder/tools/__init__.py`；`src/tricoder/session_runtime.py` | `rewrite`：Hcode 的工作区内路径设计未采用；路径、权限、生命周期和审计按 TriCoder 独立重写 | Phase 3 / 已完成 |
| MCP | `hcode/mcp/client.py`；`hcode/mcp/manager.py`；`hcode/mcp/loading_strategy.py`；`hcode/mcp/tool_wrapper.py`；`hcode/tools/mcp_call.py`；`hcode/tools/impl/tool_search.py` | Phase 4 已完成默认关闭的严格 MCP 声明、Extension Host 与来源/风险感知的动态 ToolRegistry；尚无 SDK、连接或 server 进程 | `src/tricoder/mcp/client.py`；`src/tricoder/mcp/manager.py`；`src/tricoder/mcp/tool_adapter.py`；`src/tricoder/extensions/host.py` | `rewrite`：只参考生命周期和命名；进程、网络、schema、审批、审计和结果上限必须重写 | Phase 4 已完成宿主边界；Phase 5 未实施 |
| Skills | `hcode/skills/parser.py`；`hcode/skills/loader.py`；`hcode/skills/executor.py`；`hcode/skills/install.py`；`hcode/tools/load_skill.py`；`hcode/tools/install_skill.py` | 已有默认关闭、工作区内相对目录的 `SkillsConfig` 与 Extension Host prompt 聚合边界；尚无发现、解析或加载 | `src/tricoder/skills/models.py`；`src/tricoder/skills/parser.py`；`src/tricoder/skills/loader.py`；`src/tricoder/extensions/host.py` | `adapt`：可适配清单、解析和渐进加载；不迁移自动安装器，Skill 不能扩大权限 | Phase 4 已完成宿主配置；Phase 6 未实施 |
| 项目指令加载 | `hcode/memory/instructions.py`；`hcode/agents/parser.py` | 仅有 `src/tricoder/agent.py` 内建 prompt 与配置，没有受控的项目指令/include 加载器 | `src/tricoder/instructions.py`；`src/tricoder/skills/loader.py`；`src/tricoder/agent.py` | `adapt`：适配查找/include 算法，同时限制根目录并拒绝链接/junction 逃逸 | Phase 6 / 已映射，未实施 |
| Hooks | `hcode/hooks/events.py`；`hcode/hooks/models.py`；`hcode/hooks/conditions.py`；`hcode/hooks/loader.py`；`hcode/hooks/engine.py`；`hcode/hooks/executors.py` | 已有默认关闭的 `HooksConfig`、Extension Host 和不可覆盖的动态工具网关；尚无 Hook 事件或引擎 | `src/tricoder/hooks/models.py`；`src/tricoder/hooks/engine.py`；`src/tricoder/extensions/host.py`；`src/tricoder/tools/__init__.py`；`src/tricoder/audit.py` | `rewrite`：Hcode shell/template/HTTP 执行器不能成为旁路；动作必须重新进入统一策略链 | Phase 4 已完成宿主配置；Phase 7 未实施 |
| Worktree | `hcode/worktree/models.py`；`hcode/worktree/manager.py`；`hcode/worktree/session.py`；`hcode/worktree/changes.py`；`hcode/worktree/integration.py`；`hcode/worktree/setup.py`；`hcode/worktree/cleanup.py`；`hcode/tools/enter_worktree.py`；`hcode/tools/exit_worktree.py` | 无 Worktree 服务；已有 Git 命令策略、工作区真实路径边界、Session 和 ChangeJournal | `src/tricoder/worktree/models.py`；`src/tricoder/worktree/service.py`；`src/tricoder/subprocess_control.py`；`src/tricoder/policy.py`；`src/tricoder/session_runtime.py`；`src/tricoder/sessions.py` | `rewrite`：Hcode 裸 `subprocess`、复制本地配置、symlink 与自动删除逻辑不可复制 | Phase 8 / 已映射，未实施 |
| 子 Agent | `hcode/agents/parser.py`；`hcode/agents/loader.py`；`hcode/agents/fork.py`；`hcode/agents/task_manager.py`；`hcode/agents/tool_filter.py`；`hcode/agents/trace.py`；`hcode/tools/agent_tool.py` | 无子 Agent；`CodingAgent`、`ToolRegistry`、权限快照和审计可作为宿主基础 | `src/tricoder/agents/models.py`；`src/tricoder/agents/coordinator.py`；`src/tricoder/agents/task_manager.py`；`src/tricoder/tools/agents.py` | `rewrite`：先单层只读；能力、预算、深度和取消只能收紧或继承，不能扩大 | Phase 9–10 / 已映射，未实施 |
| 团队任务与消息 | `hcode/teams/models.py`；`hcode/teams/mailbox.py`；`hcode/teams/shared_task.py`；`hcode/teams/coordinator.py`；`hcode/teams/protocol.py`；`hcode/teams/manager.py`；`hcode/teams/registry.py`；`hcode/teams/spawn_inprocess.py`；`hcode/teams/transcript.py`；`hcode/tools/send_message.py`；`hcode/tools/task_create.py`；`hcode/tools/task_get.py`；`hcode/tools/task_list.py`；`hcode/tools/task_stop.py`；`hcode/tools/task_update.py` | 无团队调度或 mailbox；Session 锁不是多 Agent 协调器 | `src/tricoder/agents/task_manager.py`；`src/tricoder/agents/coordinator.py`；`src/tricoder/agents/messages.py`；`src/tricoder/changes.py` | `rewrite`：采用有界并发、单写者/Worktree 隔离、消息大小与收件人校验；不启用 tmux/iTerm 外部后端 | Phase 10 / 已映射，未实施 |
| TUI 集成 | `hcode/app.py`；`hcode/tui.py`；`hcode/styles.tcss` | `src/tricoder/tui.py` 已有 Textual UI、审批、会话选择、折叠轮次和权限显示 | `src/tricoder/tui.py`；`src/tricoder/ui.py`；`src/tricoder/commands.py`；`src/tricoder/shell.py` | `reference`：只参考事件呈现和状态层次，不复制 Hcode App 状态管理 | Phase 11 / 已映射，当前部分具备 |
| Eval | `docs/evaluation-plan.md`（Hcode 无独立 Eval 运行模块） | `src/tricoder/evals/` 与 `evals/smoke/` 已有隔离副本、隐藏 verifier、报告和资源边界 | `src/tricoder/evals/`；`evals/` 平台能力场景 | `reference`：仅吸收能力场景与指标；继续使用 TriCoder Eval 安全模型 | Phase 11 / 已映射，现有框架待扩充 |
| Session | `hcode/memory/session.py`；`hcode/conversation.py`；`hcode/app.py` | `src/tricoder/sessions.py` 使用 SQLite 保存最小结构化记忆；`src/tricoder/session_runtime.py` 管理原子状态 | `src/tricoder/sessions.py`；`src/tricoder/session_runtime.py`；`src/tricoder/context/`；`src/tricoder/worktree/` | `rewrite`：不迁移 Hcode 完整自由文本 Session JSONL，只增加恢复所需的最小结构化引用 | Phase 2、3、8、11 / 已映射，当前部分具备 |
| Remote | `hcode/remote.py` | 无 Remote/WebSocket 服务 | 无 | `reference`：仅登记来源，不设计、不实现、不引入网络监听 | 排除 / 明确不在本次迁移范围 |

## 安全、测试与依赖清单

| Capability | 必须保留或收紧的安全边界 | Hcode 可参考测试文件 | TriCoder 计划新增或修改的测试文件 | 前置依赖 |
| --- | --- | --- | --- | --- |
| 流式 Provider | 响应字节上限、超时、错误去敏、完整 tool-call 组装、Provider 差异封装 | `tests/test_openai_compat_stream.py`；`tests/test_streaming_batching.py`；`tests/test_agent.py` | `tests/test_provider_streaming.py`；`tests/test_providers.py` | Phase 1 事件/预算契约；不新增包 |
| 类型化 Agent 事件 | 事件载荷不可携带未过滤凭据；工具参数审计仍按现有脱敏规则 | `tests/test_agent.py`；`tests/test_tui_app.py`；`tests/test_tui_components.py` | `tests/test_core_events.py`；`tests/test_agent.py`；`tests/test_tui.py` | 无第三方依赖 |
| 异步运行与取消 | 取消幂等；不得在写入中点留下伪成功；锁、审批快照与会话归属保持原子 | `tests/test_agent.py`；`tests/test_subagent.py`；`tests/test_tui_app.py` | `tests/test_cancellation.py`；`tests/test_agent.py`；`tests/test_session_runtime.py`；`tests/test_cli.py`；`tests/test_tui.py` | Phase 1 事件与取消令牌 |
| token 上下文预算 | 未知 token 不当作 0；硬上限保守失败；预算不能被扩展或子 Agent 放大 | `tests/test_context.py`；`tests/test_context_window.py` | `tests/test_budgets.py`；`tests/test_context_manager.py`；`tests/test_agent.py` | Provider usage/模型窗口信息 |
| 上下文压缩 | system 指令和最新任务固定；工具调用/结果成对；失败有熔断且不得静默丢上下文 | `tests/test_context.py`；`tests/test_context_window.py`；`tests/test_toolresult_wiring.py`；`tests/test_tool_cache_marker.py` | `tests/test_context_manager.py`；`tests/test_agent.py` | token 预算、类型化事件 |
| 大工具结果落盘 | 仅写 `runtime/`；拒绝链接/junction；原子写、容量/寿命上限；预览脱敏；回读再鉴权 | `tests/test_context.py`；`tests/test_toolresult_wiring.py` | `tests/test_context_spill.py`；`tests/test_tools.py`；`tests/test_session_runtime.py` | Context Manager、WorkspacePolicy |
| MCP | 默认关闭；初版仅 stdio；服务命令显式允许；子进程环境去敏；schema/输出有界；工具调用统一审批审计 | `tests/test_mcp.py`；`tests/test_mcp_call.py`；`tests/test_tool_search.py` | `tests/test_mcp_dependency_boundary.py`；`tests/test_mcp_manager.py`；`tests/test_mcp_tool_adapter.py`；`tests/test_tools.py`；`tests/test_audit.py` | Phase 4 Extension Host；候选 `mcp>=2.1.1,<2.2` 尚未获安装批准 |
| Skills | 只发现允许根目录；拒绝链接逃逸和超大文件；front matter 严格 schema；不自动下载/安装/执行 | `tests/test_skills.py` | `tests/test_skill_parser.py`；`tests/test_skill_loader.py` | Phase 4 Extension Host；YAML 方案尚未获安装批准 |
| 项目指令加载 | include 只读、深度/数量/总字节有界；路径真实解析；禁止读取敏感目录 | `tests/test_subagent.py` 中 include/agent 场景 | `tests/test_instructions.py`；`tests/test_skill_loader.py`；`tests/test_agent.py` | Skills 模型与加载器 |
| Hooks | 默认关闭；Hook 不直接执行 shell/HTTP/Agent；请求动作重新进入 ToolRegistry 与审批；禁止模板注入 | `tests/test_hooks.py` | `tests/test_hooks.py`；`tests/test_audit.py` | Phase 4 Extension Host、类型化事件 |
| Worktree | 不自动删除；真实路径和 reparse point 校验；Git 参数白名单；本地配置/密钥不复制；用户确认清理 | `tests/test_worktree.py` | `tests/test_worktree_service.py`；`tests/test_worktree_integration.py`；`tests/test_sessions.py`；`tests/test_tui.py` | Hooks/MCP 权限契约稳定；Subprocess Control |
| 子 Agent | 默认单层只读；权限/工具/预算单调收紧；有界时间与输出；取消传播；每个动作可归因审计 | `tests/test_subagent.py` | `tests/test_agent_permissions.py`；`tests/test_agent_coordinator.py`；`tests/test_subagent_tools.py`；`tests/test_subagent_writes.py` | Phase 5–8 契约稳定；Context/Extension Host/Worktree |
| 团队任务与消息 | 最大并发/深度/队列/消息大小；任务所有权；单写者或独立 Worktree；禁止外部 pane 进程旁路 | `tests/test_teams.py`；`tests/test_team_protocol.py`；`tests/test_coordinator_multi_team.py`；`tests/test_teammate_registry.py` | `tests/test_agent_concurrency.py`；`tests/test_agent_messages.py`；`tests/test_subagent_writes.py` | Phase 9 单层子 Agent、Worktree |
| TUI 集成 | UI 只消费类型化事件；动态内容按纯文本；审批与取消不能阻塞/绕过 Runtime | `tests/test_tui_app.py`；`tests/test_tui_components.py` | `tests/test_tui.py`；`tests/test_ui.py`；`tests/test_commands.py` | Phase 1–10 公共接口稳定 |
| Eval | fixture 不含凭据/网络；隐藏 verifier 后注入；隔离目录、资源上限和确定性验证保持不变 | Hcode 无对应自动化 Eval 测试；`docs/evaluation-plan.md` 仅作场景来源 | `tests/test_eval_platform_suite.py`；现有 `tests/test_eval_*.py` | 待评能力均完成；无需新增依赖 |
| Session | 最小持久化、事务/锁、敏感摘要、工作区绑定；不保存完整自由文本与工具原文 | `tests/test_recovery.py`；`tests/test_memory.py`；`tests/test_consolidation.py` | `tests/test_session_runtime.py`；`tests/test_sessions.py`；`tests/test_context_manager.py`；`tests/test_worktree_integration.py` | Context 与 Worktree 状态模型 |
| Remote | 不开放监听端口、不迁移鉴权/WebSocket 状态、不加入配置 | 无专用测试 | 无 | 无；排除项 |

## Phase 0 结论

- 上述 17 项覆盖任务要求中的全部能力，并与设计规范第 8、13 节一致。
- 所有列出的 Hcode 路径已在基线提交工作树中核对存在；Hcode 未被修改。
- TriCoder 已由用户选择 MIT，并建立根 `LICENSE`、`NOTICE` 和项目许可证元数据；
  后续 `adapt`/copy 必须按具体文件更新 `NOTICE`。
- Remote 和完整自由文本 Session JSONL 明确排除；Hcode 裸 subprocess、权限
  执行链和 Worktree 删除逻辑明确禁止复制。
- Phase 1、Phase 2、Phase 3 与 Phase 4 已完成；最新证据分别见
  `docs/migration/phase-1-verification.md` 和
  `docs/migration/phase-2-verification.md`、
  `docs/migration/phase-3-verification.md`、
  `docs/migration/phase-4-verification.md`。
