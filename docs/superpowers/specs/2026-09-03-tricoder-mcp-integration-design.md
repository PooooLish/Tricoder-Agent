# TriCoder MCP 集成设计

日期：2026-09-03

状态：对话中已选择方案 A；等待本文书面复核后再编写实施计划

关联总设计：`docs/superpowers/specs/2026-09-03-hcode-capability-migration-design.md`

## 1. 目标

Phase 5 为 TriCoder 增加最小、可用且安全优先的 MCP 客户端能力：

- 连接已经安装在本机的 MCP stdio server；
- 将 server 暴露的工具转换为 TriCoder 动态工具；
- 仍由现有 Tool Gateway 统一完成参数验证、审批、审计和输出治理；
- 在交互式会话、一次性 CLI 与 Agent 异步执行链中真正可用；
- 任一 server 故障不能破坏其他 server 或内置工具；
- 关闭任务时确定性回收连接和子进程。

本阶段不支持远程 HTTP/SSE、OAuth、MCP resources、prompts、server 动态安装、后台常驻连接或跨任务连接复用。

## 2. 已选择方案

采用方案 A：任务级生命周期。

每次 Coding Task 在同一个异步事件循环内依次完成：

```text
读取可信配置
  -> 请求 MCP server 启动审批
  -> 启动已批准的 stdio server
  -> initialize / list_tools
  -> 注册本任务的 MCP 工具
  -> 运行 CodingAgent
  -> finally 中注销工具、关闭会话并回收进程
```

连接不保存在 Session 间，也不由后台线程维护。代价是每次任务都有启动开销；收益是所有权清楚、取消和回收路径短、不会把失效连接或扩展权限带入下一任务。

没有启用 MCP 时，现有 Agent、ToolRegistry 和 SessionRuntime 路径保持不变，且不要求导入或安装 MCP SDK。

## 3. 设计边界

### 3.1 信任边界

MCP server 是经用户批准后运行的本地不可信子进程。MCP 协议不是安全边界，server 返回的名称、描述、Schema、文本、错误和结构化内容都按不可信输入处理。

TriCoder 的既有工作区策略只能约束 TriCoder 自己执行的工具，不能约束 MCP server 子进程内部的文件、网络或进程访问。因此界面和文档不得把本功能描述为 OS 级沙盒。

### 3.2 唯一执行入口

模型不得直接持有 MCP SDK 对象，也不得绕过 ToolRegistry 调用 server。MCP 工具必须先转换为 `ToolHandler`，再走统一异步执行入口。审批、参数验证、输出截断/溢写和安全错误转换均发生在该入口。

server 启动不是模型可调用工具，而是 Extension Host 的生命周期操作。它仍须使用与工具审批等价的受控请求、同一审批器和结构化审计，但不会出现在模型工具列表中。

### 3.3 初始风险等级

所有 MCP 工具初始统一标记为 `dangerous`。server 自报的注解只能作为显示元数据，不能降低风险等级，也不能触发自动批准。

MCP server 每次启动都必须获得明确人工批准，包括 `fullaccess` 和 relaxed 模式。该请求必须走独立的“强制审批”入口，不能复用可能自动放行的普通权限判断。后续阶段可基于本地可信清单引入更细策略，但不属于本 MVP。

## 4. 配置模型

项目配置继续由 Phase 4 的 Extension Host 发现。每个 MCP 配置只接受以下字段：

```toml
[extensions]
enabled = true

[mcp]
enabled = true

[[mcp.servers]]
id = "local-example"
transport = "stdio"
enabled = true
command = "example-mcp-server"
args = ["--stdio"]
credential_env = ["EXAMPLE_API_KEY"]
```

约束如下：

- `command` 必须是纯可执行文件名，不接受绝对路径、相对路径或路径分隔符；
- 复用 `trusted_path_executable` 的受控 PATH 解析规则：只检查绝对 PATH 条目、只接受普通可执行文件，并拒绝 symlink/reparse point；
- 使用 `shell=False`，工作目录固定为当前项目工作区；
- 参数数量、单项长度和总长度均有上限，拒绝 NUL；
- 拒绝已知的运行时下载/安装形式，例如 `npx -y`、`pip install`、`uv run --with`；
- 看起来是路径的参数必须通过 WorkspacePolicy；普通非路径 token 仍会显示在启动审批中；
- 启动审批展示 server id、解析后的可执行文件绝对路径和经过长度限制的参数，不展示环境变量值；
- 配置上限沿用 Extension Host 的 32 个扩展上限。

允许调用已经安装的解释器和项目内脚本并不等于信任脚本。只要能够执行项目代码，仍按 `dangerous` 请求逐任务审批。

## 5. 环境变量

server 仅获得满足以下条件的凭据变量：

1. 名称列在该 server 的 `credential_env`；
2. 名称同时列在可信进程级 `TRICODER_EXTENSION_ENV_ALLOWLIST`；
3. 变量在当前进程中实际存在。

Provider 密钥和未声明环境变量不得透传。审计仅记录变量名和“已提供/缺失”，绝不记录值。

官方 Python SDK 的 stdio transport 会将一组平台默认环境变量与调用方传入环境合并。为避免无意继承个人路径和用户信息，适配层必须显式覆盖 SDK 默认集合中的每个名称：仅保留运行 Windows 子进程必要的 `SystemRoot`、`PATH`、`PATHEXT`、临时目录等最小值，其余个人身份或主目录字段置空。SDK 使用精确版本固定，升级时必须重新核对默认集合。

## 6. 依赖选择与供应链

选择官方 `modelcontextprotocol/python-sdk` 发布的 PyPI 包 `mcp==2.1.1`，不安装 `[cli]` extra。

选择依据：

- PyPI 标记 Python 3.10+、MIT、Production/Stable，并提供 Trusted Publishing 证明；
- 2.1.1 是 2026-08-25 发布的当前稳定版；
- 官方安全公告中已公开的高危影响区间均不包含 2.1.1；
- v2 已移除本阶段不采用的旧 WebSocket transport；
- `mcp-types==2.1.1` 与 SDK 保持精确同步。

实施时的依赖动作：

- 在 `pyproject.toml` 中精确固定 `mcp==2.1.1`；
- 生成 `requirements.lock`，记录 Windows/Python 3.11 环境实际解析出的完整精确版本集合；
- 记录 wheel 哈希和 PyPI attestation 证据；
- 不把该文件宣传为跨平台或全哈希可重现锁；Linux/macOS 与 Python 3.12 由后续 CI 阶段补证；
- 安装依赖前必须再次取得用户明确批准。

当前参考证据：

- PyPI：https://pypi.org/project/mcp/
- v2.1.1 release：https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.1.1
- v2.1.1 manifest：https://github.com/modelcontextprotocol/python-sdk/blob/v2.1.1/pyproject.toml
- MIT license：https://github.com/modelcontextprotocol/python-sdk/blob/v2.1.1/LICENSE
- security advisories：https://github.com/modelcontextprotocol/python-sdk/security/advisories
- dependency policy：https://github.com/modelcontextprotocol/python-sdk/blob/main/DEPENDENCY_POLICY.md

## 7. 组件设计

### 7.1 SDK 边界

新增 `src/tricoder/mcp/`，所有第三方 SDK 类型都封装在该目录内。TriCoder 其他模块只依赖内部数据类和协议。

```text
src/tricoder/mcp/
  __init__.py
  client.py        # 单个 stdio server 的 SDK 生命周期
  manager.py       # 多 server 启停、隔离、路由和状态
  tool_adapter.py  # 工具名、Schema、输入输出转换
  runtime.py       # 单个 Coding Task 的异步作用域与运行时接线
```

SDK 未安装且配置未启用 MCP 时不报错。启用 MCP 但 SDK 缺失时，在启动 Provider 请求前返回固定、可操作且不含内部异常的配置错误。

### 7.2 MCPClient

`MCPClient` 只管理一个 server：

```python
class MCPClient:
    async def start(self, cancellation: CancellationToken) -> None: ...
    async def list_tools(self, cancellation: CancellationToken) -> tuple[MCPToolSpec, ...]: ...
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
        cancellation: CancellationToken,
    ) -> MCPCallResult: ...
    async def stop(self) -> None: ...
```

它负责启动审批、最小环境、stdio transport、initialize、请求超时、取消桥接、安全错误归一化和关闭。stderr 直接写入 OS null sink，不在内存、日志或审计中保留原文。

SDK 参数必须设置 `encoding_error_handler="replace"`，缓解已知的 malformed UTF-8 导致客户端退出问题。协议解析错误被转换为固定类别，原始 server 输出和异常不得穿透到 Agent、UI 或审计。

### 7.3 MCPManager

`MCPManager` 按配置顺序启动 server，并维护显式状态：

```text
disabled -> starting -> ready -> stopping -> stopped
                    \-> failed
```

要求：

- 单个 server 拒绝审批、启动失败、初始化失败或工具清单无效时，只隔离该 server；
- 其他 server 和内置工具继续工作；
- 相同任务中不做隐式重启；下一任务按任务级生命周期自然重连；
- `stop_all()` 幂等，按启动顺序反向关闭；
- 部分启动后发生取消或异常，也必须关闭所有已启动 server；
- 关闭超时后尝试 SDK 提供的进程树终止路径，并记录固定错误类别。

### 7.4 MCPToolHandler

现有同步 `ToolHandler.run()` 无法安全承载同事件循环中的 MCP 调用，因此扩展为：

```python
class ToolHandler:
    def run(self, arguments: dict[str, object]) -> ToolResult: ...

    async def run_async(
        self,
        arguments: dict[str, object],
        *,
        cancellation: CancellationToken | None = None,
    ) -> ToolResult: ...
```

默认 `run_async()` 使用 `asyncio.to_thread()` 调用既有同步 handler，保持现有工具兼容。`MCPToolHandler` 只实现原生异步路径；若被错误地从同步入口直接调用，返回固定的“仅支持异步执行”失败，不创建新事件循环。

ToolRegistry 的同步与异步入口必须复用同一组参数校验、审批、审计、输出治理和错误归一化辅助函数，避免两条安全策略漂移。

## 8. 工具名与 Schema

### 8.1 名称映射

公开工具名格式为：

```text
mcp__<server-id>__<tool-name>
```

名称统一为小写，仅保留当前 ToolRegistry 接受的字符，长度不超过 64。超过长度时使用稳定截断加短哈希，确保同一配置跨任务得到同一名称。

同一 server 的两个原始名称若规范化后冲突，必须同时拒绝并记录安全元数据，不能采用“先到先得”。不同 server 由 server id 命名空间隔离。

### 8.2 支持的 JSON Schema 子集

MVP 支持有明确资源上限的递归子集：

- `object`、`array`、`string`、`integer`、`number`、`boolean`、`null`；
- `properties`、`required`、`additionalProperties: false`；
- 标量 `enum`；
- 数组 item Schema；
- 字符串、数组和数值的简单边界约束。

拒绝 `$ref`/`$defs`、外部引用、组合器、条件 Schema、`patternProperties`、未知类型，以及超过 Schema 字节数、递归深度、属性数或数组深度上限的定义。运行时参数按同一子集递归验证。

这意味着部分由 Pydantic 生成且依赖 `$defs` 的合法 MCP 工具在 MVP 中会被安全拒绝。界面应显示“Schema 不受支持”，不能静默降级为不校验。

## 9. 输出治理

只有文本内容进入模型上下文。图片、音频、资源链接和嵌入资源转换为不含 payload 的元数据占位符，例如内容类型、条目数和是否省略；不得将 base64、二进制 blob 或任意资源正文直接注入模型。

单次工具结果使用现有 ToolResult 和 spill store 上限：

- 先对每个内容块和总结果施加字符/字节上限；
- 超限文本按现有溢写机制放入工作区允许的运行态目录；
- Agent 只收到有界摘要和受控引用；
- server 返回的 `isError` 转换为失败 ToolResult，不泄露 SDK repr、堆栈或原始 transport 错误。

官方 SDK 当前 stdio 实现会在协议对象解析前读取完整 JSON-RPC 行，因此适配层的截断不能理论上限制恶意超长单行造成的峰值内存。此风险由“本地 server、每任务显式危险审批、连接短生命周期”降低，但不能宣称已消除。

## 10. 超时、取消与清理

initialize、list_tools、call_tool 和 shutdown 分别使用有界超时。取消桥接通过事件循环内的短周期异步轮询任务监听现有 `CancellationToken`，并与目标 operation 竞争：

- 取消发生时取消 operation；
- 在 `finally` 中取消并等待监听任务；
- 清理使用受控 shield 和独立短超时；
- 不遗留后台 task、线程 watcher、stdio task 或 server 进程。

任务取消、Agent 异常、Provider 异常和 CLI 中断都进入同一个清理出口。无法确认回收时，本任务不能报告“成功完成”，而应返回固定的 cleanup failure 类别。

## 11. SessionRuntime 与 CLI 接线

Phase 5 不以“客户端模块单测通过”为完成标准，必须接入两条用户路径。

### 11.1 交互式 SessionRuntime

当当前项目启用了 MCP：

1. 为本任务创建新的 ToolRegistry，并复用当前会话的 ToolContext、审计、change journal 和 spill store；
2. 在同一异步事件循环内启动 MCP task scope；
3. 将已就绪的 MCPToolHandler 注册到临时 registry；
4. 使用当前 Provider 配置创建任务级 CodingAgent；
5. 复用原 SessionContext 执行任务；
6. 在 `finally` 中关闭 scope，临时 registry 和动态工具不进入下一任务。

未启用 MCP 时继续使用当前长期存在的 Agent，不引入额外对象重建或行为变化。

### 11.2 一次性 CLI

一次性 CLI 使用同一 `mcp.runtime` task-scope helper，不能复制一套启停逻辑。CLI 退出、Ctrl+C 和错误退出均等待清理结束或触发有界强制终止。

### 11.3 Extension Host

配置加载阶段只发现 MCP descriptor，不创建长期存活的 Extension Host。每个任务创建新的 MCP provider 和 Extension Host：`start()` 在任务 scope 内执行强制审批和真实 server 启动，`stop()` 在同一 scope 的 `finally` 中回收资源。Host 实例及其 provider、handler 缓存都不进入下一任务。

## 12. 错误模型与审计

对外只暴露稳定类别，例如：

- `mcp_sdk_missing`
- `mcp_start_rejected`
- `mcp_executable_untrusted`
- `mcp_initialize_timeout`
- `mcp_schema_unsupported`
- `mcp_tool_timeout`
- `mcp_protocol_error`
- `mcp_cleanup_failed`

审计允许记录 server id、公开工具名、原始工具名的不可逆摘要、状态、时长、参数键集合、结果大小和固定错误类别。不得记录参数值、环境变量值、原始 stderr、原始异常、完整 server 返回或二进制内容。

## 13. 测试策略

测试先于实现，全部使用 fake SDK/session/process，不依赖真实 MCP server 或网络。

最低回归集合：

1. SDK 未安装且 MCP 未启用时零影响；启用时安全报错；
2. 纯名 executable 解析、qualified path 拒绝、安装器参数拒绝；
3. 环境最小化，不泄露 Provider key、HOME/USERPROFILE 等个人路径；
4. 每任务启动与结束、反向关闭、幂等关闭；
5. 多 server 中一个失败，其余工具仍注册；
6. 取消发生在启动、list_tools、call_tool 和 Agent 阶段时均无遗留任务；
7. 工具名稳定、截断稳定、冲突时双拒绝；
8. 支持的递归 Schema 正确验证，不支持/超限 Schema 安全拒绝；
9. 所有 MCP 工具均为 dangerous，relaxed/fullaccess 不自动放行；
10. 同步入口不能运行 MCP handler，异步入口与现有审批/审计一致；
11. 超长文本经 spill store 有界返回，图片/resource/blob 不进入上下文；
12. malformed UTF-8 配置使用 replace，协议错误不泄露原文；
13. SessionRuntime 第二个任务不保留第一个任务的动态工具或连接；
14. 一次性 CLI、交互会话和 Ctrl+C 均走同一清理路径；
15. MCP disabled 时现有全量测试结果不变。

集成测试可提供一个仓库内 fake stdio server 脚本，但不得在测试中动态安装、下载或访问网络。

## 14. 开源复用边界

Hcode 仅作为只读参考：借鉴客户端/管理器/工具包装器的职责分离，以及“部分 server 失败隔离”和名称前缀测试场景。TriCoder 不复制 Hcode 的 HTTP transport、环境合并、原始异常日志或直接暴露 SDK 进程的实现。

如实施时实质复制 Hcode 代码，必须逐文件确认许可证并更新 NOTICE；预期方案是依据官方 SDK API 与 TriCoder 现有安全边界独立实现。

`docs/open-source-assessment.md` 将在实施计划获批后更新，明确选择 `reference`，而非 `integrate` 或 `fork` Hcode。

## 15. 实施顺序与回滚

详细实施计划尚未生成。预期采用以下可独立回滚的切片：

1. 依赖清单、锁定证据与可选导入边界；
2. 异步 ToolHandler/ToolRegistry 共用安全管线；
3. MCP Schema、名称和输出纯转换层；
4. 单 server client；
5. 多 server manager 与 task scope；
6. SessionRuntime/CLI/ExtensionHost 接线；
7. 文档、fake server 集成测试和全量验证。

每一片都必须先写失败测试、再做最小实现，并运行 focused tests、全量 tests、compileall、现有 eval dry-run、workspace doctor 与 diff 自审。

回滚不删除用户数据：撤销 MCP 配置即可回到完全不导入 SDK 的旧路径；代码回滚按上述切片逐项完成。MCP 不新增持久化连接或包含凭据的状态文件。

## 16. 验收标准

Phase 5 只有同时满足以下条件才算完成：

- 至少一个 fake stdio server 可从 SessionRuntime 与一次性 CLI 被 Agent 调用；
- server 启动和每次危险工具调用都经过既有审批体系；
- 取消、超时、拒绝和部分失败均有确定性、安全且可测试的行为；
- 任务结束后没有动态 MCP 工具、异步任务或子进程遗留；
- MCP disabled 路径无需 SDK 且保持兼容；
- 依赖来源、版本、许可证、已知风险和锁定局限已记录；
- 全量测试与工作区健康检查通过，所有平台未验证项如实列出。

## 17. 已知剩余风险

- stdio 单行在 SDK 解析前缺少可配置硬上限，恶意 server 仍可能造成瞬时内存压力；
- 每任务重连增加延迟，启动慢的 server 体验较差；
- JSON Schema 仅实现安全子集，可能拒绝合法工具；
- 精确版本只稳定 Python API，不代表供应链绝对安全；
- 当前依赖锁首先覆盖 Windows/Python 3.11，跨平台可重现性尚待 CI 补证；
- 没有 OS 级沙盒，获批 server 仍拥有调用用户进程所拥有的系统权限。

这些风险必须在 README/项目状态中明确展示，不能通过更宽松的默认值隐藏。
