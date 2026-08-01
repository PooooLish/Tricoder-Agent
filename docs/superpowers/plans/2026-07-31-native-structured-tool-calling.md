# TriCoder Provider 原生 Structured Tool Calling 实施计划

> **执行要求：** 使用 `superpowers:test-driven-development` 逐项实施；每项先运行新增测试并确认按预期失败，再添加最小实现。完成全部任务后使用
> `superpowers:verification-before-completion` 做最终验证。未经用户明确授权，不执行 Git 提交或推送。

**目标：** 将 OpenAI、DeepSeek、GLM 的默认工具协议迁移到 Provider 原生
`tools` / `tool_calls`，保留显式 `legacy_json` 回退，并让 Agent 核心保持厂商无关。

**架构：** Agent 使用统一的 `Message`、`ToolDefinition`、`ToolCall` 和
`ProviderResponse`。`ToolRegistry` 是工具 Schema 与处理器的单一来源。
OpenAI-compatible Provider 负责厂商负载转换，Provider Factory 根据配置选择适配器和
能力档案。Agent 根据 `tool_protocol` 选择原生或旧版循环，但不做静默降级。

**技术栈：** Python 3.11、标准库 `dataclasses` / `json` / `urllib`、
`unittest`，现有 Rich CLI。

---

## Task 1：建立 Provider 无关的结构化领域模型

**文件：**

- 修改：`src/tricoder/models.py`
- 新增：`tests/test_models.py`

### Step 1：先写失败测试

覆盖：

- `ToolDefinition` 拒绝空名称和非对象参数 Schema。
- `ToolCall` 保存调用 ID、工具名和字典参数。
- `ProviderResponse` 可以同时表达普通文本、零个或多个工具调用和结束原因。
- `Message` 可以表达普通文本、assistant tool calls 和 tool result。
- tool result 缺少 `tool_call_id`、普通消息携带 `tool_call_id` 等非法组合被拒绝。
- Message 估算字符数时包含工具名、调用 ID 和参数，而不只计算 `content`。

### Step 2：运行测试并确认失败

```powershell
python -m unittest tests.test_models -v
```

预期：因为新类型和消息字段尚不存在而失败。

### Step 3：添加最小实现

在 `models.py` 增加：

```python
@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
```

扩展 `Message`：

- `content` 改为 `str | None`；
- 增加 `tool_calls: tuple[ToolCall, ...]`；
- 增加 `tool_call_id: str | None`；
- 用 `__post_init__` 校验 role 与字段组合；
- 增加统一的字符预算计算方法；
- 不在模型层保存任何厂商原始响应对象。

### Step 4：运行测试并确认通过

```powershell
python -m unittest tests.test_models -v
```

### Step 5：运行受影响的旧测试

```powershell
python -m unittest tests.test_agent tests.test_sessions tests.test_session_runtime -v
```

预期：旧普通消息行为保持兼容；若暴露结构化消息尚未接入的失败，只记录并在对应任务解决。

---

## Task 2：让 ToolRegistry 成为工具定义和参数校验的单一来源

**文件：**

- 修改：`src/tricoder/tools.py`
- 修改：`tests/test_tools.py`

### Step 1：先写失败测试

覆盖：

- Registry 暴露稳定、只读、顺序固定的七个 `ToolDefinition`。
- 每个 Schema 使用 `type: object`、`properties`、`required` 和
  `additionalProperties: false`。
- Schema 与现有处理器名称完全一致。
- 缺少必填参数、参数类型错误和额外参数在处理器调用前失败。
- `describe(name)` 只返回注册表中的静态公开说明。
- 未知工具仍返回安全的 `ToolResult`，不抛出动态分发异常。

### Step 2：运行测试并确认失败

```powershell
python -m unittest tests.test_tools -v
```

### Step 3：添加最小实现

- 新增内部 `_ToolRegistration`，同时保存 `ToolDefinition` 和 handler。
- 用一份注册表替代当前单独的 `_handlers` 字典。
- 提供 `definitions`、`contains(name)` 和 `describe(name)` 公共接口。
- 实现满足当前七个简单 Schema 所需的本地校验：
  - object；
  - required；
  - string / integer / boolean；
  - additionalProperties。
- 不引入 `jsonschema` 依赖。
- `execute` 在校验通过后才调用现有 handler。
- Agent 和审计后续不得继续读取私有 `_handlers`。

### Step 4：运行测试并确认通过

```powershell
python -m unittest tests.test_tools -v
```

---

## Task 3：实现统一 Provider 契约和厂商能力档案

**文件：**

- 修改：`src/tricoder/providers.py`
- 修改：`tests/test_providers.py`

### Step 1：先写失败测试

建立一组公共契约测试，覆盖：

- `ModelProvider.complete(messages, tools)` 返回 `ProviderResponse`。
- assistant tool call 转换为带 `tool_calls` 的请求消息。
- tool result 转换为带 `tool_call_id` 的 `role=tool` 消息。
- 响应中的 `function.arguments` JSON 字符串转换为字典。
- 无效 arguments、缺少 ID、缺少工具名、错误响应结构抛出
  `ProviderProtocolError`。
- 普通文本响应保持为 `ProviderResponse(content=...)`，由 Agent 决定是否纠错。
- HTTP 重试行为保持不变。

分别覆盖厂商负载：

- OpenAI：发送 tools、`tool_choice=auto`、`parallel_tool_calls=false`，并在能力允许时
  发送 strict Schema。
- DeepSeek：发送 tools，不依赖 Beta strict。
- GLM：只发送其支持的 `tool_choice=auto`，省略未声明支持的强制或并行字段。

### Step 2：运行测试并确认失败

```powershell
python -m unittest tests.test_providers -v
```

### Step 3：添加最小实现

增加：

```python
@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    native_tool_calling: bool
    strict_tool_schema: bool = False
    parallel_tool_calls: bool = False
    forced_tool_choice: bool = False
    streaming: bool = False


class ProviderProtocolError(ProviderError):
    """厂商响应能收到，但无法转换成统一协议。"""
```

- 将 `ModelProvider.complete` 改为接收消息和工具定义并返回统一响应。
- 在 OpenAI-compatible 基类中集中实现：
  - 统一消息到请求 JSON；
  - ToolDefinition 到 function tool；
  - 原生 tool calls 到统一 ToolCall；
  - content 和 finish reason 提取；
  - HTTP 重试。
- 使用不可变的 Provider profile 描述请求差异。
- 当 `tools` 为空时不发送 `tools`、`tool_choice` 或并行字段，供旧版协议复用。
- 不记录请求头和 API Key。

### Step 4：运行测试并确认通过

```powershell
python -m unittest tests.test_providers -v
```

---

## Task 4：新增可扩展 Provider Factory

**文件：**

- 修改：`src/tricoder/providers.py`
- 修改：`src/tricoder/cli.py`
- 修改：`src/tricoder/session_runtime.py`
- 修改：`tests/test_providers.py`
- 修改：`tests/test_cli.py`
- 修改：`tests/test_session_runtime.py`

### Step 1：先写失败测试

覆盖：

- Factory 根据 `ProviderConfig.name` 选择 openai、deepseek、glm 档案。
- 未注册厂商明确失败。
- CLI 一次性运行和 SessionRuntime 都使用同一个默认 Factory。
- 注入自定义 Provider Factory 的现有测试能力不回归。
- Agent、SessionRuntime 和 CLI 中没有按厂商名称决定协议字段的分支。

### Step 2：运行测试并确认失败

```powershell
python -m unittest tests.test_providers tests.test_cli tests.test_session_runtime -v
```

### Step 3：添加最小实现

- 新增 `create_provider(config, timeout)`。
- 将三家能力档案注册到 Provider 层私有映射。
- CLI 和 SessionRuntime 的默认工厂改为 `create_provider`。
- 保留构造函数注入点，未来新厂商可注册完全不同的 `ModelProvider` 实现。
- 不让 Agent 访问 ProviderConfig.name。

### Step 4：运行测试并确认通过

```powershell
python -m unittest tests.test_providers tests.test_cli tests.test_session_runtime -v
```

---

## Task 5：增加显式 tool protocol 配置

**文件：**

- 修改：`src/tricoder/models.py`
- 修改：`src/tricoder/config.py`
- 修改：`src/tricoder/session_runtime.py`
- 修改：`.env.example`
- 修改：`tests/test_config.py`
- 修改：`tests/test_session_runtime.py`

### Step 1：先写失败测试

覆盖：

- 默认值为 `native`。
- `TRICODER_TOOL_PROTOCOL=legacy_json` 可以显式启用兼容模式。
- 大小写、空白和未知值不能静默接受。
- `.tricoder.toml`、环境变量和 CLI/运行时覆盖顺序与现有配置规则一致。
- Session 重建和 `/model` 切换不会丢失协议配置。

首版不增加公开 CLI 参数，避免同时扩大 CLI 表面；本地回滚通过 `.env.local` 或进程环境
变量完成。如果实现过程中发现现有配置层天然支持 CLI 覆盖，再单独提交设计变更确认。

### Step 2：运行测试并确认失败

```powershell
python -m unittest tests.test_config tests.test_session_runtime -v
```

### Step 3：添加最小实现

- `AppConfig` 增加 `tool_protocol: str = "native"`。
- 配置加载器只接受 `native` 和 `legacy_json`。
- `SessionRuntime` 构造 Agent 时透传 `loaded.tool_protocol`。
- `.env.example` 只写示例值，不写真实 Key。

### Step 4：运行测试并确认通过

```powershell
python -m unittest tests.test_config tests.test_session_runtime -v
```

---

## Task 6：用原生 ToolCall 驱动 Agent，同时保留旧版兼容路径

**文件：**

- 修改：`src/tricoder/agent.py`
- 修改：`tests/test_agent.py`

### Step 1：先写原生模式失败测试

新增结构化 ScriptedProvider，覆盖：

- Agent 将 `tools.definitions` 传给 Provider。
- 单个 ToolCall 被执行，assistant 消息保留调用，tool 消息使用相同 ID。
- 普通文本且没有调用时，追加受控纠错消息并允许下一轮修正。
- 多个调用时一个也不执行，并要求下一轮只选一个。
- ProviderProtocolError 可纠错，但 HTTP、认证等 ProviderError 仍安全停止。
- 未知工具通过正常 assistant/tool 消息对返回错误。
- `finish` 保持现有完成与验证判定。
- observer 使用静态工具描述生成公开 `ToolAction.reason`。
- 审计不访问 `_handlers`，不记录完整 arguments。

### Step 2：运行原生模式测试并确认失败

```powershell
python -m unittest tests.test_agent.NativeToolCallingTests -v
```

### Step 3：添加最小原生循环

- 将系统提示拆成公共安全规则、原生工具规则和旧版 JSON 规则。
- 原生模式调用 `provider.complete(request_messages, tools.definitions)`。
- 恰好一个 ToolCall 时构造内部 `ToolAction`，`reason` 使用
  `tools.describe(call.name)`。
- 先追加 assistant tool call，再执行工具，再追加 `role=tool` 结果。
- 对普通文本、多调用和可恢复协议错误追加脱敏纠错反馈。
- 不尝试从普通文本中寻找 JSON。
- 保持最大轮数、写后验证、审计失败停止和 Session 回滚行为。

### Step 4：运行原生测试并确认通过

```powershell
python -m unittest tests.test_agent.NativeToolCallingTests -v
```

### Step 5：先写旧版回归测试

覆盖：

- `legacy_json` 模式调用 Provider 时传空 tools。
- 只解析 `ProviderResponse.content`。
- 现有非法 JSON 自我修正行为保持不变。
- 原生模式不会调用 `parse_action`。

### Step 6：添加最小兼容实现并运行全部 Agent 测试

```powershell
python -m unittest tests.test_agent -v
```

---

## Task 7：升级上下文压缩和 Session 回合识别

**文件：**

- 修改：`src/tricoder/agent.py`
- 修改：`tests/test_agent.py`
- 修改：`tests/test_session_integration.py`

### Step 1：先写失败测试

覆盖：

- 原生 assistant/tool 消息对被视为完整回合。
- tool_call_id 不匹配的两条消息不是完整回合。
- 压缩永远不留下孤立 assistant 调用或孤立 tool 结果。
- 旧版 assistant/user(kind=tool_result) 消息对仍可压缩。
- 字符预算包含结构化 arguments。
- 两个 Session 的结构化上下文互不串联。
- 清空、切换和模型切换保持结构化内存快照。

### Step 2：运行测试并确认失败

```powershell
python -m unittest tests.test_agent tests.test_session_integration -v
```

### Step 3：添加最小实现

- 提取 `_is_complete_tool_round(first, second)`。
- 同时识别：
  - 原生 assistant tool call + tool result；
  - 旧版 assistant JSON + user tool_result。
- 原生模式额外校验 tool_call_id 一致。
- 所有上下文预算统一调用 Message 的结构化字符估算。
- 不改变 SQLite 仅持久化安全摘要的现有策略。

### Step 4：运行测试并确认通过

```powershell
python -m unittest tests.test_agent tests.test_session_integration -v
```

---

## Task 8：更新用户文档和脱敏诊断

**文件：**

- 修改：`README.md`
- 修改：`.env.example`
- 视测试结果修改：`src/tricoder/cli.py`
- 修改：`tests/test_cli.py`

### Step 1：先写失败测试

覆盖 CLI 或 doctor 的公开输出：

- 显示当前协议为 `native` 或 `legacy_json`。
- 不显示 API Key。
- Provider 协议错误使用可理解的中文提示。

### Step 2：运行测试并确认失败

```powershell
python -m unittest tests.test_cli -v
```

### Step 3：更新实现和文档

README 增加：

- 原生 structured tool calling 工作方式；
- 三家 Provider 支持情况；
- `TRICODER_TOOL_PROTOCOL` 配置；
- 显式回滚方法；
- 新 Provider 适配器扩展说明；
- 不在日志或命令输出中粘贴密钥的提醒。

### Step 4：运行测试并确认通过

```powershell
python -m unittest tests.test_cli -v
```

---

## Task 9：完整回归、自审和三个真实 API 冒烟测试

**文件：**

- 视失败情况小步修改对应源码和测试
- 运行时输出仅写入项目既有的忽略目录或 `runtime/`

### Step 1：运行完整离线测试

```powershell
python -m unittest discover -s tests -v
```

预期：全部通过，测试不得读取 `.env.local` 或访问网络。

### Step 2：运行编译检查

```powershell
python -m compileall -q src tests
```

### Step 3：运行差异检查

```powershell
git diff --check
git status --short
```

检查：

- 没有真实密钥、响应正文或私有数据进入差异；
- Agent 核心没有厂商分支；
- 原生路径没有文本 JSON 解析；
- Registry Schema 和 handlers 一致；
- tool_call_id 始终成对；
- 旧版回退只能显式启用。

### Step 4：分别运行三个真实 API 的只读冒烟测试

使用项目现有安全配置加载逻辑，不直接读取或打印 `.env.local` 内容。三个 Provider 使用同一
任务，例如列出测试目录、读取一个无敏感信息的示例文件并 `finish`。

每次只报告：

- Provider 和模型；
- 是否收到原生 tool call；
- 调用的工具名；
- 最终成功状态；
- 脱敏错误类型。

不得报告：

- Authorization 头；
- API Key；
- 完整请求体中的用户私有内容；
- 完整工具输出。

### Step 5：运行受控写入冒烟测试

在独立的 `test/` 工作区创建一个无敏感信息的小文件，走现有人工审批并运行验证命令。
不得覆盖已有文件。测试后保留结果供用户检查，不做未授权删除。

### Step 6：最终审查

使用 `superpowers:requesting-code-review` 审查本次差异；修复明确问题后重新执行 Step 1–3。
若真实 API 某家失败，记录可复现的 Provider、HTTP/协议错误类型和限制，不以旧版模式
静默替代原生测试结果。
