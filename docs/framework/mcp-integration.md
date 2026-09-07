# MCP 集成长期规则

## 生命周期与启动

- MCP 连接、动态工具和相关异步资源仅属于单个 Coding Task；任务结束时必须在
  `finally` 中反向关闭，不跨任务或 Session 复用。
- 每次启动 MCP server 都必须请求明确人工批准；`strict`、`relaxed` 与
  `fullaccess` 均不能自动放行启动。
- 首期只允许经配置显式启用的本地 stdio client，不支持 HTTP、SSE、OAuth、
  resources 或 prompts。
- 生产路径由 TriCoder 的 `VerifiedStdioTransport` 直接持有 server 进程句柄。
  `MCPTransportOutcome` 分别记录 direct process 退出证据与自持资源关闭结果；
  EOF、管道关闭、owner task 完成或终止调用返回都不能单独证明进程已退出。
- 只有观察到直接进程非 `None` 的 returncode（允许非零）且自持流/任务关闭才允许
  `STOPPED`。无法证明时保持 `FAILED` 与固定 `mcp_cleanup_failed`，重复 stop
  不能抹去失败，也不能覆盖主业务异常或原始取消。
- flush 关闭失败/超时、运行期 reader/writer 非正常退出保持黏着故障；最终资源
  关闭成功仍可记 `resources_closed=True`，但不能把该次故障改回 VERIFIED。
  spawn 前已分配端点的关闭失败同样保留 client→manager→Host 的清理责任。
- 成功只证明直接 server 进程和 TriCoder 自持资源已收尾，不保证所有后台化、
  脱离进程组的后代都已消失；平台进程树终止仍是尽力而为，不是 OS 沙盒。

## 信任与数据边界

- MCP server 是经明确批准运行的本地不可信代码和未隔离子进程，拥有与
  TriCoder 进程对应用户相同的权限。ToolRegistry 约束的是 TriCoder 侧调用链，
  不能约束 server 内部的文件、网络或进程访问；不得将 MCP 描述为 OS 沙盒。
- 所有 MCP 工具固定标记为 `dangerous`；server 提供的注解只能用于展示，不能
  降低风险等级或绕过审批。
- `credential_env` 声明的凭据及额外变量，只有在进程级
  `TRICODER_EXTENSION_ENV_ALLOWLIST` 同时允许且实际存在时才会注入 server。最小
  OS/runtime 启动变量（例如 `PATH`、`SYSTEMROOT`，或 POSIX 对应项）仍按安全启动
  需要保留；不得透传 Provider 密钥、未声明的额外变量或个人环境默认值。日志和审计
  只记录变量名及状态。
- 仅接受有资源上限的 JSON Schema 子集，并对参数、文本输出、结构化内容和
  错误信息施加大小与深度限制。二进制、资源正文和原始 stderr 不进入模型、
  UI 或审计。

## SDK 与回滚

- 第三方 `mcp` SDK 只能由 `tricoder.mcp` 适配层按需导入；MCP 未启用的路径
  不得导入 SDK 或调用加载器。
- SDK 缺失或协议失败时，对外返回稳定、可操作且不含原始异常的错误。
- 适配器精确绑定 `mcp==2.1.1`（协议类型 `mcp-types==2.1.1`）。升级必须重新
  完成能力接口、日志来源、生命周期及依赖/安全评审；必需能力或源码身份缺失时，
  在任何 server 进程创建前失败关闭。
- Windows adapter 从已验证的 exact SDK `WeakKeyDictionary` 转移该进程的 Job
  handle；终止使用但不释放 handle，只有不吞异常的 `win32api.CloseHandle`
  正常返回后才移除 adapter 所有权。失败 handle 保持强引用；这不是自动重试或
  任意后代清理保证。POSIX 无 Job；SDK 未交付 Job 时 adapter 不声称拥有该资源。
  asyncio transport 由独立版本绑定 wrapper 直接 close，存在 `is_closing` 时必须
  得到 True。SDK 的 best-effort void close helper 不能作为确定性关闭证据。
- 启动前检查必需 Python helper 的实际参数签名与 async/sync 形状、协议构造器/
  session 方法及 Windows mapping/API；无 signature 的两个 pywin32 C API 仅接受
  已锁定的 native 名称形状。此检查不证明任意被修改依赖实现的运行时语义。
- 日志隔离只覆盖已加载 SDK 的精确 logger 名与词法规范化绝对 pathname，且必须
  位于对应 task-local `ContextVar` 作用域。包含实际 `client` session logger、
  stdio、两个 dispatcher 和可用平台 utilities 来源；不按 module 或正文分类。
  命中记录在宿主 filters/handlers 格式化前整条丢弃；不改 root、level、disabled、
  handlers 或 propagate，也不删除宿主新增 filter。
- 临时 filter view 引用 exact 原宿主 list，SDK overlay 逻辑上位于 index 0；
  每次遍历取稳定快照，标准 `addFilter/removeFilter` 更新同一底层 list，最终恢复
  exact list identity。若宿主整体替换 `logger.filters`，保留新容器且不再保证该
  logger 的隔离；任意自定义容器或直接切片/排序等 list 操作不在协调协议范围内。
- lifecycle、list/call 请求与可延迟结束的 SDK 清理 helper 各自保持日志租约。
  最后租约退出只移除 exact 自身 filter；旁路 task、旁路线程及不同 pathname 的
  同名 logger 保持宿主原有行为。禁用 MCP 的路径不导入 `mcp`、`mcp_types` 或 `anyio`。
- 回滚首先禁用项目 `[mcp]` 配置，使现有路径恢复为不导入 SDK；连接不持久化，
  因而不需要清理凭据或会话状态。

## Phase 5 已知边界与交接

- 支持范围固定为本地 `stdio`。不支持远程 MCP、HTTP/SSE/OAuth、resources、
  prompts 或自动下载、安装 server；新增 transport 或安装行为必须另行安全评审。
- `credential_env` 只保存变量名；声明的凭据及额外变量仅在可信进程级
  `TRICODER_EXTENSION_ENV_ALLOWLIST` 同时授权时才会传入 server。安全启动所需的
  最小 OS/runtime 变量仍会保留。TOML、日志和审计绝不保存或回显凭据值。
- Schema 仅支持受限的 JSON Schema 子集；未知类型、关键字或超限结构都必须
  显式拒绝，不能降级为未验证调用。
- 本地 stdio 管道仍有残余风险：单行消息过大时，本地 transport/底层管道的缓冲与
  JSON 解析可能先于 TriCoder 的结果边界消耗大量内存。现有输出处理不是对
  任意超大单行消息的端到端保证。
- `requirements.lock` 是本轮 Windows、Python 3.11 环境的可观察锁定结果，不能
  证明 Linux/macOS 或其他 Python 小版本的可复现性；跨平台矩阵留待后续验证。
- 当前兼容性证据仅来自离线内存协议测试、受控进程契约和仓库本地 fake server；
  真实外部 MCP server、真实 Provider、Linux/macOS 和 Python 3.12 仍未验证。
