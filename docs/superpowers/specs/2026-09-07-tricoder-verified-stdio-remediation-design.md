# TriCoder 可验证 stdio 与 SDK 日志隔离设计

日期：2026-09-07
状态：已批准（2026-09-07）
上位规格：`docs/superpowers/specs/2026-09-03-tricoder-mcp-integration-design.md`

## 1. 背景与目标

Phase 5 的最终复审仍有两个相互独立、但都位于 MCP SDK 边界的问题：

1. 锁定的 `mcp==2.1.1` 在 kill 后仍未观察到 server 进程退出时只写 warning，
   随后正常退出 stdio 上下文。TriCoder 因而会把“句柄和协程已收尾”误当成
   “进程已确认退出”。
2. SDK 的会话实现使用通用 logger 名 `client`；现有过滤器只覆盖
   `mcp.client.session`，因此无效 notification 的 Pydantic 异常仍可能把原始协议值
   写入宿主日志。

本轮目标是在不修改虚拟环境、不升级或安装依赖、不访问真实 Provider/外部 MCP
server 的前提下，为当前已批准的本地 stdio MCP 建立可测试的结构化清理证据，并
精确阻断锁定 SDK 的原始协议日志。MCP 未启用路径继续不得导入第三方 SDK。

## 2. 方案选择

选择用户批准的方案 A：TriCoder 独立实现一个最小的、受控的 stdio transport，
继续使用官方 SDK 的 `ClientSession`、协议类型及锁定版本的跨平台进程辅助能力。

不采用以下方案：

- 不把所有当前 SDK 清理都判为失败；那会使 MCP 功能不可用。
- 不根据 warning 文本决定安全状态；日志可能被禁用、改写或提前过滤，且文本不是
  结构化生命周期接口。
- 不在生产环境 monkeypatch SDK 的模块全局函数、logger 变量或 async generator
  frame；这些方式会污染同进程其他使用者并形成并发竞态。
- 不复制、修改或 vendoring `.venv` 中的 SDK 源码。

本地 transport 按协议与现有公开对象独立实现消息桥接；它通过一层锁定版本适配器
调用 SDK 的进程创建、进程树终止和 Windows Job 清理能力。私有/半公开符号的存在与
形状在 `load_mcp_sdk()` 中集中校验；不满足时在启动任何进程前失败关闭。版本升级
必须重新审查该适配器，不能静默沿用。

## 3. 组件与接口

### 3.1 `tricoder.mcp.transport`

新增只属于 MCP 适配层的 transport 模块。模块顶层只依赖 Python 标准库和
TriCoder 自有类型；MCP/AnyIO 对象由按需加载器注入，避免关闭路径提前导入 SDK。

核心契约：

```python
class MCPProcessExitEvidence(str, Enum):
    NOT_STARTED = "not_started"
    VERIFIED = "verified"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MCPTransportOutcome:
    process_exit: MCPProcessExitEvidence
    resources_closed: bool


class VerifiedStdioTransport(AsyncContextManager[tuple[Any, Any]]):
    @property
    def outcome(self) -> MCPTransportOutcome: ...
```

`VerifiedStdioTransport` 私有持有 exact process handle、内存消息流、pipe bridge
任务和平台清理适配器。只有同时满足以下条件才能产生 `VERIFIED`：

- server 直接进程的 `returncode is not None`；退出码可以为 `0` 或非零；
- 本 transport 创建的 stdin/stdout 与内存流已关闭；
- 本 transport 创建的 reader/writer 任务已退出或被有界取消并收割；
- Windows Job handle 或对应平台资源已经执行确定性关闭。

EOF、上下文管理器返回、owner task 完成、pipe close 成功或 terminate/kill 调用返回，
都不能单独把证据升级为 `VERIFIED`。任何异常、超时或 kill 后仍观察不到退出都得到
`UNKNOWN`；transport 仍继续尽力关闭其余资源，但不得把状态改回成功。

### 3.2 锁定 SDK 进程适配器

`load_mcp_sdk()` 在真正启用 MCP 时才加载并验证：

- `ClientSession`、`StdioServerParameters` 与协议消息类型；
- AnyIO 内存流/任务组能力；
- 当前平台的进程创建、进程树终止与 Windows Job 清理函数；
- 当前实际会产生日志的 SDK 模块源文件身份。

这些对象封装进扩展后的 `MCPSDK` 数据对象，由 transport 和日志边界消费。生产
`MCPClient` 使用本地 `VerifiedStdioTransport` 类与 `MCPSDK.stdio_bindings` 显式构造
transport；SDK 数据对象本身不负责创建 transport，也不把官方 `stdio_client` 暴露为
成功判据。SDK 对象不得扩散到 Agent、ToolRegistry、SessionRuntime 或配置层。适配器
只支持已锁定的 `mcp==2.1.1` 契约；关键能力缺失或形状不符时抛固定的
`MCPDependencyError`，不回显底层异常。

AnyIO 已由精确锁定的 `mcp==2.1.1` 运行时依赖保证，本轮不增加 manifest 项、
不重建 lockfile，也不安装任何包。

### 3.3 `MCPClient` 生命周期

生产 `MCPClient` 改为消费 `VerifiedStdioTransport`，而不是把官方
`stdio_client` 正常退出直接等价为清理成功。`AsyncExitStack` 退出后必须检查
transport outcome：

- `VERIFIED + resources_closed`：允许进入 `STOPPED`；
- `UNKNOWN`、`NOT_STARTED` 与已启动事实矛盾，或 `resources_closed=False`：设置
  黏着 `_cleanup_failed`，进入 `FAILED`，并产生固定
  `MCPCleanupError("mcp_cleanup_failed")`；
- 启动从未创建进程且没有待清理资源时，`NOT_STARTED` 可以作为普通启动失败收尾，
  但不能伪装成一次成功运行。

重复 `stop()` 必须保留未知/失败证据。主业务异常或原始 `CancelledError` 仍保持原样
越过 runtime；清理失败通过 manager/runtime 的既有安全审计和后续 stop 路径可见，
但不能覆盖主异常。

### 3.4 精确 SDK 日志隔离

保留现有 `ContextVar + RLock + 引用计数` 结构，过滤条件改为三者同时满足：

1. 当前 task 位于 TriCoder SDK 调用作用域；
2. `record.name` 是按需加载器登记的实际 logger；
3. 经过纯词法规范化的 `record.pathname` 与该 logger 对应 SDK 模块的预计算绝对源
   路径完全相同。

至少覆盖当前 stdio 路径的 `client`、`mcp.client.stdio`、
`mcp.shared.jsonrpc_dispatcher`、`mcp.shared.dispatcher`、
`mcp.os.posix.utilities` 与 `mcp.os.win32.utilities` 的真实来源。通用名 `client`
只有在记录确实来自锁定 SDK 的 `session.py` 时才被过滤。

过滤器在每个目标 logger 的过滤器序列首部安装，只移除自身 exact 对象；不得改变
logger/root 的 level、disabled、handlers 或 propagate，也不得删除宿主运行期间新增的
过滤器。过滤命中后整条记录在格式化前丢弃，不调用 `record.getMessage()`，不检查或
保存原始正文。旁路 task、旁路线程、不同 pathname 的同名 logger 必须保持原行为。

SDK 路径发现只能发生在 MCP 已启用且调用 `load_mcp_sdk()` 之后；普通 import、doctor
关闭配置及其他非 MCP 路径继续保持冷导入。

## 4. 数据流与关闭顺序

启动：

1. 既有安全策略完成 command、cwd、env、参数和人工审批。
2. 按需加载并验证锁定 SDK 适配器。
3. transport 创建 exact process，随后创建有界职责的 reader/writer bridge。
4. 官方 `ClientSession` 消费 transport 提供的内存消息流并 initialize。

关闭：

1. 停止接受新请求，关闭写入方向并给已接受消息一个固定 flush 窗口。
2. 关闭 server stdin，给 server 固定自然退出窗口。
3. 未退出则调用平台进程树终止；在固定 reap 窗口内轮询 direct process
   `returncode`。
4. 无论结果如何，关闭 Job、stdout、底层 transport 与内存流，并收割 bridge 任务。
5. 组合 `process_exit` 与 `resources_closed`，将不可证明状态保留为 `UNKNOWN`。
6. `MCPClient` 把 UNKNOWN 转为黏着清理失败；manager/runtime 执行既有反向收尾。

所有等待仍受现有客户端总清理预算约束；实现计划必须用各阶段常量之和证明默认总预算
足够，不能再用两层相同 timeout 形成调度竞态。

## 5. 错误与安全边界

- 对模型、UI 和审计只公开固定错误码；不得回显原始 JSON、ValidationError、argv、
  环境值、stderr、绝对可执行路径或 SDK 异常文本。
- server stderr 继续进入系统 null sink；SDK 自身原始日志由精确来源过滤器阻断。
- 本设计确认的是 direct server 进程以及本 transport 自持资源。平台进程树终止仍为
  尽力而为，daemonize/脱离进程组等场景不在保证内；不得描述为 OS 沙盒。
- 单行超大 JSON 在解析前的峰值内存风险保持为已知限制；本轮不把 transport remediation
  扩成通用流量整形项目。
- 不支持 HTTP/SSE/OAuth、远程 MCP、自动下载或安装 server。

## 6. 测试设计

所有生产修改遵循 RED→GREEN：先加入能在当前代码上因目标缺陷失败的测试并实际确认
失败，再写最小实现。

### Transport 单元/契约测试

- 进程自然退出、terminate 后退出、kill 后退出：均观察到非 `None` returncode，
  outcome 为 VERIFIED；非零退出码同样属于已回收。
- kill 后 returncode 始终为 `None`：outcome 为 UNKNOWN，所有可关闭资源仍被关闭。
- EOF、pipe close、owner 完成但进程未退出：不得产生 VERIFIED。
- 启动失败前/后、reader/writer 异常、flush/退出/reap timeout、native cancel 与 token
  cancel 均有有界收尾测试。
- 两个并发 transport 的 process/outcome 互不污染。

### Client/manager/runtime 集成测试

- UNKNOWN 使 `stop()` 固定失败且重复 stop 仍失败；list/call 不再可用。
- 真实 `MCPClient → MCPManager → run_mcp_task` 成功操作遇到 UNKNOWN 时最终失败。
- 有主异常或取消时不覆盖原异常身份，但清理失败仍有 metadata-only 审计证据。
- 正常仓库 fake server 完整启动、调用、spill 回读与停止后得到 VERIFIED。

### 日志隔离测试

- 真实 `_parse_line → ClientSession/JSONRPCDispatcher → _on_notify` 路径携带合成非法
  notification；无保护对照能捕获 sentinel，保护内 direct/root/stderr 均捕获不到。
- `mcp.shared.dispatcher` 受控 intercept 异常路径同样不泄漏。
- 同名 `client`、`mcp.client.stdio` 的第三方记录以及相同 module 名、不同 pathname 的
  记录仍可见。
- 重叠/嵌套 client、异常退出、取消、线程与旁路 task 验证引用计数和 ContextVar 隔离；
  退出后只恢复自身过滤器，不覆盖宿主新增配置。
- 新进程冷导入测试证明禁用 MCP 时没有加载 `mcp` 或 `anyio`。

### 验证门禁

- 新增/相关 focused tests；
- 全部 MCP tests；
- 完整 `unittest discover`；
- `compileall`、`git diff --check`；
- CP936/UTF-8 离线 doctor、eval dry-run、workspace doctor；
- 不调用真实 Provider、外部 MCP 或用户密钥。

## 7. 开源、依赖与文档

实现前更新 `docs/open-source-assessment.md`：官方 SDK 的实际复用方式为
`integrate`，本地 transport orchestration 为 `greenfield`，只调用已安装依赖提供的
协议对象和锁定版本进程辅助接口，不复制 SDK 源码。本轮不改变 MIT 许可证或依赖集合；
如实现过程中发现必须复制或实质改编上游代码，立即停止并重新评估 NOTICE、来源范围和
用户授权。

完成后同步 `docs/framework/mcp-integration.md`、README 与 `project.md`，准确说明：
TriCoder 使用自持进程句柄的本地 stdio adapter；验证只覆盖 direct process 和自持
资源，不宣称完整 OS 沙盒或任意后代清理保证。

## 8. 非目标

- 不升级 MCP SDK，不修改 manifest/lockfile，不引入新依赖。
- 不实现 remote transport、server 自动安装、协议代理或通用进程沙盒。
- 不处理既有 deferred minor（分页、Windows env casefold、argv 最终长度等），除非
  新 transport 直接触及且不处理会破坏本设计。
- 不提交、不推送；Git 操作仍需单独明确授权。

## 9. 回滚

第一回滚路径仍是禁用 `[mcp]`，关闭路径不加载 SDK。代码级回滚时恢复官方
`stdio_client` 接线并删除本地 transport，但这会重新引入“无法结构化证明退出”的
已知限制，因此只能作为功能禁用后的临时回滚，不得重新标记 Phase 5 完成。
