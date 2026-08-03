# KV Cache 可观测性与稳定前缀设计

## 目标

在不改变现有上下文压缩算法、不引入厂商专属缓存生命周期配置的前提下，完成两项能力：

1. 统一采集 OpenAI、DeepSeek、GLM 返回的 Token 与缓存命中指标，并在 CLI 中逐轮展示、任务结束汇总。
2. 固化模型请求中的稳定前缀，使同一会话的追加式工具回合更容易命中厂商提供的上下文缓存。

本设计不在客户端保存 KV Cache。缓存仍由模型服务商管理，TriCoder 只负责稳定输入和观测结果。

## 范围

### 包含

- Provider 响应中的统一 Token 用量模型。
- 三家 Chat Completions 兼容响应的缓存字段解析。
- Agent 逐轮用量事件与任务级累计。
- CLI 逐轮和最终累计展示。
- 只包含数值的安全审计元数据。
- System Prompt、工具定义和请求序列化的确定性测试。

### 不包含

- 修改上下文压缩或引入语义摘要。
- OpenAI `prompt_cache_key`、延长缓存保留时间等显式缓存配置。
- 本地 tokenizer 或精确 Token 预算。
- 缓存成本估算与价格表维护。
- 将用量统计持久化到 Session SQLite。

## 架构

### 统一用量模型

在 `models.py` 中增加不可变的 `TokenUsage`。字段使用可选非负整数，避免把“厂商没有返回”误认为零：

- `input_tokens`
- `output_tokens`
- `cached_tokens`
- `cache_miss_tokens`

模型提供两个纯计算能力：合并多轮用量，以及在输入 Token 可用且大于零时计算缓存命中率。`ProviderResponse` 增加可选 `usage`，`RunResult` 增加累计 `usage`，默认值保持现有调用方兼容。

### Provider 归一化

`OpenAICompatibleProvider` 在解析正常响应内容和工具调用后，尝试解析 `usage`：

- OpenAI/GLM 风格：读取 `prompt_tokens`、`completion_tokens` 和 `prompt_tokens_details.cached_tokens`。
- DeepSeek 风格：读取 `prompt_tokens`、`completion_tokens`、`prompt_cache_hit_tokens` 和 `prompt_cache_miss_tokens`。

厂商档案决定优先读取的方言，但解析器允许兼容字段并存。可选遥测字段缺失、类型错误或为负数时忽略对应字段，不得使一个原本有效的模型响应失败。

### Agent 与 UI 数据流

Agent 每次成功取得 Provider 响应后执行两件事：

1. 将本轮 `TokenUsage` 合并进任务累计值。
2. 通过 `AgentObserver.on_provider_usage(round_number, usage)` 发出公开事件。

`NullObserver` 保持静默。CLI Observer 收到事件后停止请求中的 spinner，并显示本轮输入、缓存命中、命中率和输出 Token；未知字段显示为 `-`。最终结果面板展示累计数据。Provider 请求失败时没有虚构用量事件。

审计事件只增加轮次与非负数值字段，不保存 Prompt、任务原文、源码、工具输出、缓存键或凭据。

## 稳定前缀

稳定前缀由固定 System Prompt、固定消息顺序以及确定性工具定义构成。实现遵循以下约束：

- System Prompt 不包含时间、随机值、Session ID 或运行统计。
- 工具定义在 Provider 边界按工具名排序，不依赖注册顺序。
- JSON 对象保持固定字段结构；工具调用参数使用确定性 JSON 序列化。
- 相同输入对象必须产生相同请求负载。
- 同一任务后续轮次只在历史尾部追加消息，旧消息不得重写。
- 用量指标只进入 Observer、RunResult 和审计，不得注入下一轮 Prompt。

工具定义位于 `messages` 之外，但仍属于请求的稳定组成部分，因此测试同时比较固定消息前缀和完整工具负载。

## 错误处理

- 缺失 `usage`：返回 `usage=None`，正常执行任务。
- 部分字段缺失：保留能安全解析的字段。
- 布尔值、负数、字符串或嵌套结构错误：视为未知，不抛出 Provider 协议错误。
- 输入 Token 为零或未知：命中率为未知，避免除零。
- Observer 展示异常仍遵循现有 UI 边界，不改变工具执行结果。

## 测试策略

采用测试驱动开发，并逐项确认测试先因缺少能力而失败：

1. `TokenUsage` 合并、未知值传播和命中率测试。
2. OpenAI、DeepSeek、GLM 响应解析测试。
3. 缺失或畸形用量字段的降级测试。
4. Agent 逐轮 Observer 事件和 `RunResult` 累计测试。
5. CLI 单轮与最终累计输出测试。
6. 工具注册顺序不同但请求工具负载相同的稳定性测试。
7. 后续请求保持前一轮消息前缀不变的测试。
8. 审计日志仅包含数值且不包含敏感原文的测试。

完成后运行核心测试套件；若环境具备项目完整依赖，再运行全部单元测试。真实三家 API 的缓存命中冒烟测试不属于本次代码完成门槛，留待独立验证，避免测试过程产生不可控费用。

## 验收标准

- 三家 Provider 的已知缓存字段被归一化为同一模型。
- CLI 每轮显示缓存指标，任务结束显示累计指标。
- 无缓存指标的兼容服务仍可正常运行。
- 用量统计不改变后续 Prompt。
- 工具 Schema 和固定消息前缀具备确定性测试。
- 审计及终端输出不包含新增的任务原文、源码或凭据。
- 现有核心测试无回归。
