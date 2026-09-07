# Hcode 迁移 Phase 2 验证记录

日期：2026-09-03

范围：Provider 真流式输出、异步 Agent 规范入口、同步兼容包装，以及从 UI
到 Provider/工具的协作式取消。本阶段未接入 MCP，也未增加第三方依赖。

## 实现结果

- `OpenAICompatibleProvider.stream()` 复用现有 urllib Transport，增量解码
  UTF-8 和 SSE，并发布文本、思考、工具开始/完成、usage 与完成事件。
- 响应总字节、单 SSE 事件和累计生成输出分别有硬上限；未知扩展事件被忽略，
  无效编码、JSON、结构和超限错误均转换为不含响应正文的安全异常。
- 工具参数可以跨 chunk 组装，但只有完整结束边界才发布
  `ToolCallCompleted`。流中断或取消时，半个工具调用不会进入执行器。
- 标准 OpenAI 尾部 usage 在 `[DONE]` 前仍会被消费；统一
  `ProviderCompleted` 最后发布。阻塞 HTTP 读取优先使用 `read1()`，避免小型
  SSE chunk 等待固定缓冲区填满。
- `CodingAgent.run_with_context_async()` 是唯一规范循环；同步入口只负责创建
  事件循环，并在已有事件循环中明确拒绝嵌套。
- `SessionRuntime.cancel_current()` 通过独立状态锁原子发布和读取当前取消令牌，
  不会被任务持有的主状态锁阻塞，也不会错过令牌发布窗口。
- 取消信号覆盖规划、Provider 读取、重试退避、多工具间隙和受管命令进程树。
  取消后返回稳定的未完成结果，并为已经生成的多工具回合补齐未执行结果。
- 批量 CLI 和 Textual TUI 消费同一类型化事件；动态增量继续以 `Text` 字面
  渲染。TUI 的 `Ctrl+C` 会取消活动任务，`Ctrl+Q` 会先取消再非零退出，避免
  与任务收尾并发持久化。

以上实现围绕 TriCoder 的 Provider、Agent、ToolRegistry、SessionRuntime 和
Subprocess Control 独立重写；没有复制 Hcode 源码，因此本阶段无需新增 NOTICE
归属条目。

## TDD 与回归场景

- Provider fake byte stream：UTF-8 跨 chunk、SSE 多行 data、`[DONE]`、文本
  增量、分片工具参数、工具开始/完成顺序、尾部 usage、未知事件、单事件/累计
  输出上限、连接中断、重试退避取消和原生工具 schema。
- Agent：同步/异步结果、上下文、usage 与审计类别等价；类型化事件顺序；规划
  取消；工具间取消；同步入口拒绝嵌套事件循环。
- Runtime/UI：令牌发布竞态、跨线程取消、CLI 纯文本增量、TUI 事件桥、
  `Ctrl+C` 和活动任务退出取消。
- 子进程：真实 Python 进程在延迟副作用发生前被取消，并确认进程树清理。
- 原有 native 与 `legacy_json` 协议、会话原子状态、命令策略、文件账本和审计
  回归均包含在全量测试中。

## 最新验证证据

环境：Windows，Python 3.11.6，项目现有 `.venv`。

| 检查 | 结果 | 覆盖范围 |
| --- | --- | --- |
| `python -B -m unittest tests.test_provider_streaming tests.test_providers tests.test_agent_async tests.test_session_runtime tests.test_subprocess_control -v` | exit 0；100 tests，OK | Phase 2 核心协议与取消 |
| `python -B -m unittest discover -s tests` | exit 0；581 tests，252.453s，OK；4 skipped | 全项目回归 |
| `python -B -m compileall -q src tests` | exit 0 | 源码与测试语法/字节码编译 |
| `python -B -m tricoder eval evals\smoke --dry-run --no-color` | exit 0；3/3 validated | Eval 定义与隔离装配 |
| `workspace.py doctor tricoder-cli` | exit 0；0 findings | 项目交接契约 |
| `git diff --check` | exit 0 | 当前差异空白与冲突标记 |

4 个跳过项均为既有的平台能力测试；本阶段没有把跳过项描述为已覆盖。

## 限制与剩余风险

- urllib 的单次阻塞 socket 读取只能受请求 timeout 约束；令牌会在每次打开、
  读取和退避前检查，但不能强制中断操作系统中正在进行的那一次阻塞读取。
- 当前只验证 Windows/Python 3.11.6；Linux/macOS、Python 3.12 和真实三家
  Provider 流式响应仍未在本阶段执行。
- TUI 已线程安全显示增量，但很小的 chunk 仍会形成多个 RichLog 条目；后续
  平台 TUI 阶段可做短窗口合并，不影响 Agent 或 Provider 协议。
- MCP、token-aware Context Manager 和大工具结果落盘仍未接入；下一阶段是
  Phase 3，必须由用户再次明确批准后开始。
