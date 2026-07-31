# TriCoder Provider 原生 Structured Tool Calling 设计

## 1. 背景

TriCoder 当前要求模型返回一段包含 `tool`、`arguments` 和 `reason` 的 JSON 文本，
Agent 再对文本执行 `json.loads` 并调用工具。这个方案适合 MVP，但存在以下问题：

- 模型可能在 JSON 前后输出解释文字，导致解析失败。
- 工具定义只存在于提示词中，Provider 无法利用模型原生的工具调用能力。
- assistant 工具调用和工具结果没有使用标准的关联 ID。
- Provider 接口只返回字符串，难以扩展流式输出、多工具调用和其他厂商协议。

本轮将 OpenAI、DeepSeek 和 GLM 的默认执行路径迁移到原生 structured tool
calling，同时保留显式启用的旧版 JSON 协议，作为兼容和回滚手段。

## 2. 目标

- 三个现有 Provider 默认通过原生 `tools` / `tool_calls` 完成工具调用。
- Agent 核心不依赖 OpenAI Chat Completions 的原始数据结构。
- 工具定义、参数 Schema 和工具实现保持单一来源。
- Provider 差异封装在适配器内部，后续接入其他厂商时不修改 Agent 核心。
- 原生协议出现普通文本、未知工具或非法参数时安全失败并允许模型自我修正。
- 保留 `legacy_json` 模式，但不自动静默降级。
- 保持现有工作区隔离、人工审批、审计脱敏和 Session 独立记忆规则。

## 3. 非目标

- 本轮不实现流式工具调用。
- 本轮不执行多个工具的并行调用。
- 本轮不接入新的模型厂商。
- 本轮不引入第三方多模型 SDK。
- 本轮不改变文件写入、命令执行和人工审批的安全策略。
- 本轮不把模型的隐藏推理或自由文本当作可信工具参数。

## 4. 方案选择

采用“内部统一模型 + 厂商协议适配器”的结构。

DeepSeek 和 GLM 当前可以复用 OpenAI-compatible Chat Completions 的公共转换逻辑，
但这个兼容格式只属于 Provider 层，不能成为 Agent 的领域模型。未来接入 Anthropic
Messages、Gemini Content 等不同协议时，只新增对应适配器。

不选择每个 Provider 完全独立实现，因为会重复请求构造、响应校验和错误处理。不选择
直接引入多模型 SDK，因为当前三个接口的公共部分较小，自有适配器更能展示 Provider
抽象能力，也能避免新增运行时依赖。

## 5. 分层架构

```text
CLI / Session
     |
     v
Agent 核心
  - 统一消息
  - 统一工具调用
  - 工具执行循环
     |
     v
ModelProvider 接口
  - complete(messages, tools)
  - capabilities
     |
     +----------------+----------------+----------------+
     v                v                v                v
OpenAI Provider   DeepSeek Provider   GLM Provider   未来厂商 Provider
     |
     v
各厂商原生 HTTP 请求和响应
```

Agent 只依赖统一接口和统一数据对象，不读取厂商名称，也不包含
`if provider == "glm"` 一类分支。

## 6. 统一领域模型

### 6.1 ToolDefinition

`ToolDefinition` 描述模型可以选择的工具：

- `name`：稳定的工具名。
- `description`：面向模型的简短用途说明。
- `parameters`：JSON Schema 对象。

工具 Schema 与工具处理器由同一个 `ToolRegistry` 注册，避免提示词、Provider 和执行器
分别维护参数定义。Registry 应提供只读的工具定义列表和按名称执行工具的入口。

### 6.2 ToolCall

`ToolCall` 是 Provider 返回给 Agent 的标准化调用：

- `id`：厂商返回的调用 ID。
- `name`：工具名。
- `arguments`：已经解析为字典、但尚未执行的参数。

原始 `function.arguments` 必须先通过 JSON 解析和对象类型校验。数组、字符串、`null`
或非法 JSON 均不能进入工具执行器。

### 6.3 ProviderResponse

`ProviderResponse` 表示一次模型响应：

- `content`：可选的普通文本。
- `tool_calls`：标准化的工具调用序列。
- `finish_reason`：可选的标准化结束原因，主要用于诊断。

本轮 Agent 每轮只接受一个工具调用。零个调用会进入协议纠错流程；超过一个调用会作为
不支持的协议结果处理，不并行执行，也不悄悄丢弃额外调用。

### 6.4 ChatMessage

统一消息必须能够表达：

- system 和 user 文本消息；
- assistant 普通文本；
- assistant 发起的一个或多个 `ToolCall`；
- 带 `tool_call_id` 的 tool 结果消息。

Session 持久化和上下文压缩使用统一消息，而不是保存厂商原始响应。Provider 适配器在
请求前将统一消息转换成厂商格式。

### 6.5 ProviderCapabilities

每个 Provider 声明能力，而不是让 Agent 根据厂商名称猜测：

- `native_tool_calling`
- `strict_tool_schema`
- `parallel_tool_calls`
- `forced_tool_choice`
- `streaming`

本轮只使用 `native_tool_calling`，并允许适配器根据其他能力安全构造请求。能力对象为后续
扩展保留稳定位置，但不会提前实现流式或并行执行。

## 7. Provider 接口

Provider 对 Agent 暴露的概念接口为：

```python
class ModelProvider(Protocol):
    @property
    def capabilities(self) -> ProviderCapabilities: ...

    def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition],
    ) -> ProviderResponse: ...
```

HTTP 地址、认证头、模型名、重试和厂商负载转换继续由 Provider 层负责。

OpenAI-compatible 的公共基类可以复用：

- 消息转换；
- `tools` 转换；
- `tool_calls` 解析；
- HTTP 请求、超时和重试；
- 通用响应结构校验。

各厂商子类或配置对象负责：

- 默认 base URL；
- 模型名；
- 能力声明；
- 厂商允许的 `tool_choice`；
- 是否发送 `strict` 或 `parallel_tool_calls` 等可选字段。

## 8. 三家 Provider 行为

### 8.1 OpenAI

- 发送原生 function tools。
- 在当前模型和端点支持时，对函数定义启用严格 Schema。
- 显式关闭并行工具调用。
- 使用自动工具选择；Agent 在本地要求每轮最终选择一个工具。

### 8.2 DeepSeek

- 使用常规原生 Tool Calls 协议。
- 不默认依赖 Beta strict endpoint。
- 显式关闭并行工具调用，前提是当前端点支持该字段。
- 对生成的参数继续执行本地 JSON 和 Schema 校验。

### 8.3 GLM

- 使用 GLM 原生支持的 OpenAI-compatible tools 格式。
- `tool_choice` 使用 `auto`，不发送 GLM 当前未承诺支持的强制选项。
- 不假设服务端支持严格 Schema。
- 是否发送并行控制字段由能力声明决定；不支持时省略，并在本地拒绝多个调用。

## 9. Agent 执行循环

原生模式下每轮流程为：

1. Agent 从 ToolRegistry 获取工具定义。
2. Provider 将统一消息和工具定义转换为厂商请求。
3. Provider 将响应转换为 `ProviderResponse`。
4. Agent 校验恰好存在一个工具调用。
5. Agent 把 assistant 工具调用消息写入当前 Session。
6. ToolRegistry 校验工具名和参数后执行。
7. Agent 以 `role=tool` 和相同 `tool_call_id` 写入工具结果。
8. 下一轮把完整消息对重新发送给 Provider。
9. `finish` 工具继续作为唯一的正常结束入口。

`reason` 不再作为所有工具的伪参数传给模型。CLI 和审计需要展示动作时，使用受控的静态
工具描述以及脱敏后的执行状态，避免诱导模型输出隐藏推理，也避免把解释字段误传给工具。

## 10. 协议错误与恢复

以下结果不得执行工具：

- 没有 `tool_calls`，只有普通文本；
- 一次返回多个工具调用；
- 缺少调用 ID、工具名或参数；
- 参数不是合法 JSON 对象；
- 工具名未注册；
- 参数不满足本地 Schema。

可恢复的协议错误会转换成受控的 tool-protocol feedback，提示模型下一轮必须选择一个
已注册工具并提供合法参数。反馈不能伪造不存在的 `tool_call_id`；如果模型根本没有发起
调用，则使用普通 user 纠错消息。Agent 沿用现有最大轮数，避免无限纠错。

HTTP、认证、限流和响应结构损坏仍由 Provider 错误类型表示，不冒充工具执行结果。

## 11. 原生与旧版协议

配置增加：

```env
TRICODER_TOOL_PROTOCOL=native
```

合法值：

- `native`：默认；只使用 Provider 原生 structured tool calling。
- `legacy_json`：显式使用当前基于文本 JSON 的行为。

不会实现自动探测或静默降级。这样原生协议的集成错误能够在测试和日志中被发现，而不是
被旧行为掩盖。

旧版解析器保留在独立兼容路径中。Agent 的主要循环不得同时猜测响应究竟是 tool call
还是文本 JSON。

## 12. Session 与上下文管理

每个 Session 继续保持独立消息历史。原生工具调用引入后，一次完整工具回合由以下两条
消息组成：

1. assistant 消息，包含 ToolCall；
2. tool 消息，包含相同 `tool_call_id` 的执行结果。

上下文压缩必须把这两条消息视为不可拆分的回合。淘汰历史时不能只保留调用或只保留结果。
固定的 system 消息和原始用户任务仍优先保留。

读取旧 Session 时，需要兼容原有 assistant 文本 JSON 与 user 工具结果。旧记录可以继续
显示或在 `legacy_json` 模式下续接；不在本轮进行破坏性批量迁移。新原生 Session 使用
新的结构化消息格式。

## 13. 安全边界

- API Key 仍只从本地环境加载，不进入请求日志、审计日志或 Session 文件。
- 工具调用参数在执行前经过 JSON 对象、工具名和 Schema 三层校验。
- 未注册工具永远不能通过动态导入或字符串求值执行。
- Provider 传回的调用 ID 只作为关联标识，不作为路径、命令或文件名使用。
- 工具输出继续执行长度限制和脱敏，不把无限输出重新送入模型。
- 原生 tool calling 不扩大文件系统、命令执行或人工审批权限。
- 测试不得读取真实 `.env.local`，真实 API 冒烟测试必须由显式命令触发。

## 14. 可扩展性约束

新增厂商时应只需要：

1. 实现或组合一个 `ModelProvider` 适配器；
2. 将统一消息和工具定义转换为厂商请求；
3. 把厂商响应转换为 `ProviderResponse`；
4. 声明 `ProviderCapabilities`；
5. 注册到 ProviderFactory；
6. 通过统一 Provider 契约测试和该厂商的负载测试。

Agent、ToolRegistry、Session 和 CLI 不应因新增厂商而修改。若新厂商无法表达当前统一
语义，适配器必须明确报告不支持的能力，不能用脆弱的字符串拼接模拟工具调用。

未来的流式输出、多工具并行、视觉消息和 Prompt Caching 应通过新增能力和独立接口扩展，
不能改变现有非流式 `complete` 的语义。

## 15. 测试策略

所有行为先写失败测试，再添加最小实现。

### 15.1 统一模型和 Registry

- ToolDefinition 从 Registry 单一生成。
- 参数 Schema 与处理器保持对应。
- ToolCall 参数只接受 JSON 对象。
- 未知工具和非法参数被拒绝。

### 15.2 Provider 契约

- 三家请求均包含正确的工具定义。
- OpenAI、DeepSeek、GLM 的能力差异产生正确请求字段。
- assistant tool call 和 tool result 能正确往返序列化。
- 合法响应转换为统一 ProviderResponse。
- 普通文本、非法 arguments、缺少 ID 和多个调用被正确识别。
- 重试和 HTTP 错误行为保持现有语义。

### 15.3 Agent

- 原生工具调用可以完成读、写、命令和 finish 循环。
- Agent 不再解析原生响应中的文本 JSON。
- 协议错误可以获得一次受控纠错机会。
- 超过最大轮数安全停止。
- `legacy_json` 模式保持兼容。

### 15.4 Session 和上下文

- 不同 Session 的结构化消息互不影响。
- 压缩不会拆开 assistant/tool 消息对。
- 旧 Session 可以安全读取。
- 持久化内容不包含密钥。

### 15.5 真实 API 冒烟测试

单元测试和本地模拟测试全部通过后，分别使用 OpenAI、DeepSeek 和 GLM 执行相同的只读
工具任务，再执行一个需要审批的受控写入任务。测试只报告 Provider、模型、成功状态、
工具名和脱敏错误，不打印请求认证信息或完整敏感参数。

## 16. 验收标准

- 默认配置下，三个 Provider 都通过响应中的 `tool_calls` 驱动工具执行。
- 正常原生路径不调用旧版 `parse_action(raw_text)`。
- assistant/tool 消息通过 `tool_call_id` 正确关联。
- Agent 核心不存在按厂商名称分支的协议逻辑。
- `legacy_json` 只能通过显式配置启用。
- 现有安全策略、Session 隔离和审批行为没有回归。
- 新旧测试全部通过。
- 三个真实 API 的受控冒烟测试通过，或明确记录来自厂商端的可复现限制。

## 17. 参考资料

- OpenAI Function calling:
  https://developers.openai.com/api/docs/guides/function-calling
- DeepSeek Tool Calls:
  https://api-docs.deepseek.com/guides/tool_calls/
- DeepSeek Chat Completion API:
  https://api-docs.deepseek.com/api/create-chat-completion/
- GLM 工具调用:
  https://docs.bigmodel.cn/cn/guide/capabilities/function-calling
- GLM 对话补全:
  https://docs.bigmodel.cn/api-reference/模型-api/对话补全
