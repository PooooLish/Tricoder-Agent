# KV Cache Observability And Stable Prefix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 统一观测 OpenAI、DeepSeek、GLM 的上下文缓存命中情况，并让相同会话的模型请求具有可测试的稳定前缀。

**Architecture:** Provider 将厂商响应归一化为 TokenUsage，Agent 负责逐轮通知与任务累计，UI 只消费统一模型。请求稳定性在 Provider 边界通过工具排序和确定性 JSON 编码保证，统计信息绝不回注 Prompt。

**Tech Stack:** Python 3.11、标准库 dataclasses/json/unittest、Rich、现有 Chat Completions Provider 抽象。

## Global Constraints

- 不修改现有上下文压缩算法，不引入语义摘要。
- 不启用 prompt_cache_key、扩展缓存保留时间或其他厂商专属缓存生命周期配置。
- 不增加第三方依赖，不维护价格表，不估算缓存费用。
- 不将 Token 用量持久化到 Session SQLite。
- 可选遥测字段缺失或畸形不得使有效模型响应失败。
- 审计及终端输出只包含 Token 数值，不包含 Prompt、任务原文、源码、工具输出、缓存键或凭据。
- 保留本地 test/smoke_demo.py 和 test/test_smoke_demo.py，不得暂存、修改或提交。

## File Map

- src/tricoder/models.py：统一 TokenUsage，并挂接 ProviderResponse、RunResult。
- src/tricoder/providers.py：解析三家 usage 方言；稳定工具排序和 JSON 编码。
- src/tricoder/agent.py：逐轮事件、任务累计和安全审计。
- src/tricoder/ui.py：单轮及累计缓存指标。
- tests/test_models.py：用量模型测试。
- tests/test_providers.py：解析、降级和确定性请求测试。
- tests/test_agent.py：Observer、累计、审计及追加式前缀测试。
- tests/test_ui.py：每轮和最终展示测试。
- README.md：功能边界和指标说明。

---

### Task 1: Provider 无关的 Token 用量模型

**Files:**
- Modify: tests/test_models.py
- Modify: src/tricoder/models.py

**Interfaces:**
- Produces: TokenUsage(input_tokens, output_tokens, cached_tokens, cache_miss_tokens)。
- Produces: TokenUsage.merge(other: TokenUsage) -> TokenUsage。
- Produces: TokenUsage.cache_hit_ratio -> float | None。
- Produces: ProviderResponse.usage 与 RunResult.usage。

- [ ] **Step 1: 写入失败测试**

在 tests/test_models.py 导入 RunResult、TokenUsage 并加入：

~~~python
def test_token_usage_merges_known_values_and_computes_hit_ratio(self) -> None:
    first = TokenUsage(100, 20, 60, 40)
    second = TokenUsage(50, None, 25, None)

    total = first.merge(second)

    self.assertEqual(TokenUsage(150, 20, 85, 40), total)
    self.assertAlmostEqual(85 / 150, total.cache_hit_ratio or 0.0)

def test_token_usage_rejects_invalid_counts(self) -> None:
    self.assertIsNone(TokenUsage(cached_tokens=10).cache_hit_ratio)
    self.assertIsNone(TokenUsage(input_tokens=0, cached_tokens=0).cache_hit_ratio)
    for value in (-1, True, "10"):
        with self.subTest(value=value):
            with self.assertRaises(ValueError):
                TokenUsage(input_tokens=value)  # type: ignore[arg-type]

def test_results_accept_optional_usage(self) -> None:
    usage = TokenUsage(input_tokens=10, output_tokens=2, cached_tokens=8)
    self.assertEqual(usage, ProviderResponse(content="ok", usage=usage).usage)
    self.assertEqual(usage, RunResult(True, "ok", 1, usage=usage).usage)
~~~

- [ ] **Step 2: 运行 RED**

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'; $env:PYTHONPATH=(Resolve-Path 'src').Path; python -m unittest tests.test_models
~~~

Expected: FAIL，tricoder.models 没有 TokenUsage。

- [ ] **Step 3: 最小实现**

在 ProviderResponse 前增加：

~~~python
@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    cache_miss_tokens: int | None = None

    def __post_init__(self) -> None:
        for value in (
            self.input_tokens,
            self.output_tokens,
            self.cached_tokens,
            self.cache_miss_tokens,
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Token 用量必须是非负整数或 None")

    def merge(self, other: "TokenUsage") -> "TokenUsage":
        def add(left: int | None, right: int | None) -> int | None:
            values = [value for value in (left, right) if value is not None]
            return sum(values) if values else None

        return TokenUsage(
            add(self.input_tokens, other.input_tokens),
            add(self.output_tokens, other.output_tokens),
            add(self.cached_tokens, other.cached_tokens),
            add(self.cache_miss_tokens, other.cache_miss_tokens),
        )

    @property
    def cache_hit_ratio(self) -> float | None:
        if self.input_tokens is None or self.input_tokens <= 0:
            return None
        if self.cached_tokens is None:
            return None
        return self.cached_tokens / self.input_tokens
~~~

给 ProviderResponse 和 RunResult 最后增加 usage: TokenUsage | None = None，保持现有位置参数兼容。

- [ ] **Step 4: 运行 GREEN**

重复 Step 2 命令，Expected: OK。

- [ ] **Step 5: 提交**

~~~powershell
git add -- src/tricoder/models.py tests/test_models.py
git commit -m "feat: add provider token usage model"
~~~

---

### Task 2: 三家 Provider 用量归一化

**Files:**
- Modify: tests/test_providers.py
- Modify: src/tricoder/providers.py

**Interfaces:**
- Consumes: TokenUsage、ProviderResponse.usage。
- Produces: _ProviderProfile.usage_dialect，值为 openai 或 deepseek。
- Produces: _extract_usage(response) -> TokenUsage | None。

- [ ] **Step 1: 写入三种响应和降级失败测试**

使用以下 usage 表驱动 make_provider；响应的 choices 保持现有合法夹具：

~~~python
cases = {
    "openai": (
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 75},
        },
        TokenUsage(100, 20, 75, None),
    ),
    "deepseek": (
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_cache_hit_tokens": 70,
            "prompt_cache_miss_tokens": 30,
        },
        TokenUsage(100, 20, 70, 30),
    ),
    "glm": (
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 65},
        },
        TokenUsage(100, 20, 65, None),
    ),
}
~~~

增加畸形测试：prompt_tokens=True、completion_tokens="20"、cached_tokens=-1 时仍返回正常 content 且 usage is None。增加部分字段测试，断言只保留合法字段。

- [ ] **Step 2: 运行 RED**

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'; $env:PYTHONPATH=(Resolve-Path 'src').Path; python -m unittest tests.test_providers
~~~

Expected: FAIL，ProviderResponse.usage 为 None。

- [ ] **Step 3: 实现容错解析**

给 _ProviderProfile 增加 usage_dialect；OpenAI/GLM 使用 openai，DeepSeek 使用 deepseek。加入：

~~~python
def _optional_token_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None
~~~

将 _extract_response 改为实例方法。先解析 prompt_tokens/completion_tokens；DeepSeek 读取 prompt_cache_hit_tokens 和 prompt_cache_miss_tokens；OpenAI/GLM 从 prompt_tokens_details.cached_tokens 读取命中。四项全未知时返回 None。遥测解析与主响应协议错误隔离。

- [ ] **Step 4: 运行 GREEN**

重复 Step 2，Expected: OK。

- [ ] **Step 5: 提交**

~~~powershell
git add -- src/tricoder/providers.py tests/test_providers.py
git commit -m "feat: normalize provider cache usage"
~~~

---

### Task 3: Agent 事件、累计与安全审计

**Files:**
- Modify: tests/test_agent.py
- Modify: src/tricoder/agent.py

**Interfaces:**
- Consumes: ProviderResponse.usage、TokenUsage.merge、RunResult.usage。
- Produces: AgentObserver.on_provider_usage(round_number: int, usage: TokenUsage)。
- Produces: status 为 provider_usage 的纯数值审计事件。

- [ ] **Step 1: 写入 Observer 和累计失败测试**

扩展 RecordingObserver：

~~~python
def on_provider_usage(self, round_number: int, usage: TokenUsage) -> None:
    self.events.append(f"usage:{round_number}:{usage.cached_tokens}")
~~~

构造两轮原生调用：read_file 带 TokenUsage(100, 10, 60, 40)，finish 带 TokenUsage(50, 5, 35, 15)。断言：

~~~python
self.assertIn("usage:1:60", observer.events)
self.assertIn("usage:2:35", observer.events)
self.assertEqual(TokenUsage(150, 15, 95, 55), result.usage)
self.assertEqual(
    provider.histories[0],
    provider.histories[1][:len(provider.histories[0])],
)
~~~

同时断言 history 中没有包含 usage 数值的新消息。

- [ ] **Step 2: 运行 RED**

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'; $env:PYTHONPATH=(Resolve-Path 'src').Path; python -m unittest tests.test_agent.NativeToolCallingTests
~~~

Expected: FAIL，缺少 usage 事件或累计结果。

- [ ] **Step 3: 实现累计和事件**

循环前定义 accumulated_usage: TokenUsage | None = None。每次 Provider 成功响应后：

~~~python
if response.usage is not None:
    accumulated_usage = (
        response.usage
        if accumulated_usage is None
        else accumulated_usage.merge(response.usage)
    )
    self.observer.on_provider_usage(round_number, response.usage)
~~~

给 NullObserver 增加空方法，给任务产生的所有 RunResult 传 usage=accumulated_usage；不得向 messages 追加指标。

- [ ] **Step 4: 写入审计 RED**

用临时 AuditLogger 执行带用量任务，断言 JSONL 含 status=provider_usage、round 和四个非负数字段；断言日志不含任务、Provider content、工具输出哨兵。运行测试，Expected: FAIL，尚无该审计事件。

- [ ] **Step 5: 实现安全审计并运行 GREEN**

增加 _audit_usage，只输出非 None 的四个数值。在 Observer 通知后记录独立 provider_usage 事件。审计失败沿用安全停止，不执行工具。

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'; $env:PYTHONPATH=(Resolve-Path 'src').Path; python -m unittest tests.test_agent
~~~

Expected: OK。

- [ ] **Step 6: 提交**

~~~powershell
git add -- src/tricoder/agent.py tests/test_agent.py
git commit -m "feat: observe and aggregate cache usage"
~~~

---

### Task 4: CLI 每轮与累计展示

**Files:**
- Modify: tests/test_ui.py
- Modify: src/tricoder/ui.py

**Interfaces:**
- Consumes: TokenUsage.cache_hit_ratio、RunResult.usage。
- Produces: ConsoleUI.on_provider_usage(round_number, usage)。

- [ ] **Step 1: 写入每轮展示失败测试**

~~~python
def test_provider_usage_renders_each_round_cache_metrics(self) -> None:
    ui, console = recording_ui()

    ui.on_provider_usage(2, TokenUsage(1_000, 50, 800, 200))

    text = console.export_text()
    self.assertIn("第 2 轮用量", text)
    self.assertIn("输入 1,000", text)
    self.assertIn("缓存 800", text)
    self.assertIn("80.0%", text)
    self.assertIn("输出 50", text)
~~~

- [ ] **Step 2: 写入最终累计和未知字段失败测试**

构造带 usage 的 RunResult，分别调用 show_run_result、show_complete，断言包含“累计用量”和命中率。再使用只有 cached_tokens 的 TokenUsage，断言输入/输出显示 - 且不会除零。

- [ ] **Step 3: 运行 RED**

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'; $env:PYTHONPATH=(Resolve-Path 'src').Path; python -m unittest tests.test_ui
~~~

Expected: FAIL，缺少 on_provider_usage 或累计行。

- [ ] **Step 4: 实现统一格式化**

~~~python
def _format_token_usage(usage: TokenUsage) -> str:
    def count(value: int | None) -> str:
        return "-" if value is None else f"{value:,}"

    ratio = usage.cache_hit_ratio
    ratio_text = "-" if ratio is None else f"{ratio:.1%}"
    return (
        f"输入 {count(usage.input_tokens)} · "
        f"缓存 {count(usage.cached_tokens)} ({ratio_text}) · "
        f"输出 {count(usage.output_tokens)}"
    )
~~~

on_provider_usage 停止 spinner 后输出“第 N 轮用量 · ...”。两个最终面板仅在 result.usage 非空时增加“累计用量”行。

- [ ] **Step 5: 运行 GREEN**

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'; $env:PYTHONPATH=(Resolve-Path 'src').Path; python -m unittest tests.test_ui tests.test_agent
~~~

Expected: OK。

- [ ] **Step 6: 提交**

~~~powershell
git add -- src/tricoder/ui.py tests/test_ui.py
git commit -m "feat: display cache usage in CLI"
~~~

---

### Task 5: 确定性请求序列化

**Files:**
- Modify: tests/test_providers.py
- Modify: src/tricoder/providers.py

**Interfaces:**
- Produces: _stable_json_bytes(payload: dict[str, object]) -> bytes。
- Preserves: JsonTransport.post_json 与 ModelProvider.complete 签名。

- [ ] **Step 1: 写入工具顺序和参数 JSON 稳定性 RED**

同一 Provider 分别传入 [weather_tool, alpha_tool] 和反向顺序，断言 transport payload 的 tools 完全一致且名称顺序为 alpha、weather。构造参数键插入顺序相反的两个 ToolCall，断言 _serialize_message 生成相同的 function.arguments。

- [ ] **Step 2: 运行 RED**

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'; $env:PYTHONPATH=(Resolve-Path 'src').Path; python -m unittest tests.test_providers.ProviderTests
~~~

Expected: FAIL，顺序随输入变化。

- [ ] **Step 3: 实现稳定工具与参数序列化**

~~~python
ordered_tools = sorted(tools, key=lambda tool: tool.name)
payload["tools"] = [self._serialize_tool(tool) for tool in ordered_tools]
~~~

工具参数使用：

~~~python
json.dumps(
    call.arguments,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)
~~~

- [ ] **Step 4: 写入请求字节确定性 RED**

测试两个仅字典插入顺序不同的 payload 经过 _stable_json_bytes 后得到相同 UTF-8 字节，且中文保持 UTF-8 而非 ASCII 转义。Expected: FAIL，函数不存在。

- [ ] **Step 5: 实现稳定编码并运行 GREEN**

~~~python
def _stable_json_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
~~~

UrllibTransport 使用该函数作为 request data。

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'; $env:PYTHONPATH=(Resolve-Path 'src').Path; python -m unittest tests.test_providers
~~~

Expected: OK。

- [ ] **Step 6: 提交**

~~~powershell
git add -- src/tricoder/providers.py tests/test_providers.py
git commit -m "feat: stabilize provider request prefixes"
~~~

---

### Task 6: 文档、全量验证与自审

**Files:**
- Modify: README.md
- Verify: src/tricoder/models.py
- Verify: src/tricoder/providers.py
- Verify: src/tricoder/agent.py
- Verify: src/tricoder/ui.py

**Interfaces:**
- Documents: 指标来源、隐式缓存性质、未知值和本阶段边界。

- [ ] **Step 1: 更新 README**

说明每轮与累计指标来自服务商 usage；“-”表示服务商未返回；TriCoder 不在本地保存 KV Cache；本版本没有启用显式缓存键和延长保留策略。

- [ ] **Step 2: 运行核心完整测试**

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'; $env:PYTHONPATH=(Resolve-Path 'src').Path; python -m unittest tests.test_agent tests.test_audit tests.test_config tests.test_models tests.test_policy tests.test_providers tests.test_session_runtime tests.test_sessions tests.test_tools
~~~

Expected: OK，允许既有条件跳过，失败和错误均为 0。

- [ ] **Step 3: 在依赖可用时运行全套测试**

~~~powershell
$env:PYTHONDONTWRITEBYTECODE='1'; $env:PYTHONPATH=(Resolve-Path 'src').Path; python -m unittest discover -s tests -p 'test_*.py'
~~~

Expected: 依赖完整时为 OK；如果仅因缺失 rich 无法导入 CLI/UI，报告环境限制且不安装依赖。

- [ ] **Step 4: 检查差异与敏感边界**

~~~powershell
git diff --check
git status --short
git diff -- src/tricoder tests README.md
~~~

确认没有 .env、Token、Key、任务原文样本或本地 test/ 沙盒文件进入差异；逐项对照设计验收标准。

- [ ] **Step 5: 提交文档**

~~~powershell
git add -- README.md
git commit -m "docs: explain cache usage metrics"
~~~

- [ ] **Step 6: 输出最终证据**

报告实际测试计数、条件跳过、完整测试环境限制、提交列表和仍未执行的三家真实 API 缓存冒烟测试。未经用户再次明确要求，不推送远端。
