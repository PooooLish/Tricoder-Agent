"""同一真实 Agent 任务内搜索超时后的延迟恢复与清理失败停止。"""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from tricoder.agent import CodingAgent
from tricoder.models import MemoryConfig, ProviderResponse, SessionContext, ToolCall
from tricoder.policy import CommandPolicy, WorkspacePolicy
from tricoder.process import control
from tricoder.process.cache import CompileCache
from tricoder.task_cleanup import current_cleanup
from tricoder.tools import ToolContext, ToolRegistry


def response(call_id, tool, arguments):
    return ProviderResponse(tool_calls=(ToolCall(call_id, tool, arguments),), finish_reason="tool_calls")


class DelayedProvider:
    def __init__(self, delay=5.2):
        self.delay = delay
        self.histories = []
        self.scopes = []
        self.waited = 0
        self.responses = [
            response("create", "create_file", {"path": "ok.py", "content": "value = 42\n"}),
            response("timeout", "search_text", {"query": "(a+)+$", "use_regex": True}),
            response("search", "search_text", {"query": "value", "use_regex": True}),
            response("compile", "run_command", {"command": "python -m compileall -q ok.py"}),
            response("finish", "finish", {"summary": "已创建并验证", "outcome": "completed"}),
        ]

    def complete(self, messages, tools=()):
        self.histories.append(list(messages))
        self.scopes.append(current_cleanup())
        if len(self.histories) == 3:
            start = time.monotonic()
            while self.waited < self.delay:
                time.sleep(self.delay - self.waited)
                self.waited = time.monotonic() - start
        if not self.responses:
            raise AssertionError("意外的额外模型请求")
        return self.responses.pop(0)


class SearchTimeoutRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "input.txt").write_text("a" * 30 + "!", encoding="utf8")
        self.tools = ToolRegistry(ToolContext(WorkspacePolicy(self.root), CommandPolicy(),
            approver=lambda *_: True, timeout=10))
        self.provider = DelayedProvider()
        self.agent = CodingAgent(self.provider, self.tools, max_rounds=6, plan_enabled=False,
            memory_config=MemoryConfig(compaction="off", persistence="off"))
        self.processes = []
        self.scopes = []
        self.caches = []
        self.executed = []
        real_collect = control._collect_bounded_process
        real_cache_init = CompileCache.__init__
        real_execute = self.tools.execute_async

        def collect(process, **kwargs):
            # 只缩短首次灾难性匹配预算，正常搜索与 compileall 使用原预算。
            if kwargs.get("stdin_bytes") and b"(a+)+$" in kwargs["stdin_bytes"]:
                kwargs["timeout"] = .2
            self.processes.append(kwargs["resources"])
            self.scopes.append(current_cleanup())
            return real_collect(process, **kwargs)

        def cache_init(cache, *args, **kwargs):
            real_cache_init(cache, *args, **kwargs)
            self.caches.append(cache)

        async def execute(name, arguments, **kwargs):
            result = await real_execute(name, arguments, **kwargs)
            self.executed.append((name, result))
            return result

        for target in (patch.object(control, "_collect_bounded_process", side_effect=collect),
                       patch.object(CompileCache, "__init__", cache_init),
                       patch.object(self.tools, "execute_async", side_effect=execute)):
            target.start()
            self.addCleanup(target.stop)
        # 即使 RED/断言失败，测试宿主仍回收记录的 exact owner，绝不清空失败事实。
        self.addCleanup(self.release_resources)

    def release_resources(self):
        for scope in self.scopes:
            self.assertTrue(scope.retry(time.monotonic() + 5))
        for resource in self.processes:
            self.assertTrue(resource.cleanup(time.monotonic() + 5))
        for cache in self.caches:
            self.assertTrue(cache.cleanup(time.monotonic() + 5))

    def test_same_task_recovers_after_delayed_model_then_search_compile_and_finish(self):
        turn = self.agent.run_with_context("创建 ok.py，搜索并编译验证", SessionContext())
        result = turn.result
        self.assertGreaterEqual(self.provider.waited, 5.2)
        timeout = self.executed[1][1]
        self.assertEqual("timeout", timeout.error.code.value)
        self.assertEqual("replan", timeout.error.recovery.value)
        feedback = [m.content for m in self.provider.histories[2] if m.role == "tool" and m.tool_call_id == "timeout"]
        self.assertEqual(1, len(feedback))
        self.assertIn("timeout", feedback[0])
        self.assertIn("replan", feedback[0])
        self.assertTrue(result.ok, result)
        self.assertEqual(["create_file", "search_text", "search_text", "run_command", "finish"],
                         [name for name, _ in self.executed])
        self.assertFalse(result.cleanup_failed)
        self.assertEqual("通过", result.verification)
        self.assertEqual(5, result.rounds)
        self.assertEqual(5, len(self.provider.histories))
        self.assertTrue(self.executed[2][1].ok)
        self.assertIn("ok.py:1: value = 42", self.executed[2][1].output)
        self.assertEqual(0, self.executed[3][1].command_check.returncode)
        self.assertEqual("value = 42\n", (self.root / "ok.py").read_text(encoding="utf8"))
        self.assertEqual(3, len(self.processes))
        scope = self.scopes[0]
        self.assertIsNotNone(scope)
        self.assertTrue(all(item is scope for item in self.scopes + self.provider.scopes))
        self.assertIsNone(scope.started_deadline)
        self.assertFalse(scope.failed)
        self.assertFalse(scope.has_pending)
        self.assertFalse(self.tools.has_pending_cleanup)
        self.assertFalse(self.agent._cleanup_owner.has_pending)
        self.assertTrue(all(p.process.poll() is not None for p in self.processes))
        self.assertTrue(all(not r.is_alive() for p in self.processes for r in p.readers))
        self.assertEqual(1, len(self.caches))
        self.assertFalse(self.caches[0].path.exists())
        self.assertFalse(list(self.root.rglob("*.pyc")))

    def test_unconfirmed_search_cleanup_stops_and_retains_exact_owner(self):
        self.provider.delay = 0
        real_cleanup = control._ProcessResources.cleanup
        retained = []

        def unconfirmed(resource, deadline):
            # 实际回收进程/读写线程，但模拟退出确认失败；持有者必须仍可追踪。
            self.assertTrue(real_cleanup(resource, deadline))
            retained.append(resource)
            return False

        with patch.object(control._ProcessResources, "cleanup", unconfirmed):
            turn = self.agent.run_with_context("创建并验证", SessionContext())
        self.assertFalse(turn.result.ok)
        self.assertTrue(turn.result.cleanup_failed)
        self.assertEqual(["create_file", "search_text"], [name for name, _ in self.executed])
        error = self.executed[1][1].error
        self.assertEqual("cleanup_failed", error.code.value)
        self.assertEqual("stop_task", error.recovery.value)
        self.assertEqual(2, len(self.provider.histories))
        self.assertFalse(self.caches)
        self.assertEqual(1, len(retained))
        self.assertIs(self.processes[0], retained[0])
        scope = self.scopes[0]
        self.assertTrue(scope.failed)
        self.assertTrue(scope.has_pending)
        self.assertIsNotNone(scope.started_deadline)
        self.assertTrue(self.tools.has_pending_cleanup)
        blocked = self.agent.run_with_context("不得复用", SessionContext())
        self.assertTrue(blocked.result.cleanup_failed)
        self.assertEqual(2, len(self.provider.histories))
        retried = []

        def retry(resource, deadline):
            retried.append(resource)
            return real_cleanup(resource, deadline)

        with patch.object(control._ProcessResources, "cleanup", retry):
            self.assertTrue(scope.retry(time.monotonic() + 5))
        self.assertEqual([retained[0]], retried)
        self.assertFalse(scope.has_pending)
        self.assertTrue(scope.failed)


if __name__ == "__main__":
    unittest.main()
