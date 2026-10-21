"""正则匹配与 stdin 的真实子进程边界。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tricoder.policy import WorkspacePolicy, CommandPolicy
from tricoder.tools import ToolContext, ToolRegistry
from tricoder.process.control import run_bounded_process
from tricoder.process.control import BoundedProcessResult
from tricoder.core.cancellation import CancellationToken, CancellationError
from tricoder.task_cleanup import TaskCleanup, cleanup_scope


class SearchLimitTests(unittest.TestCase):
    def test_recoverable_search_timeout_does_not_start_task_termination(self):
        (self.root / "input.txt").write_text("a" * 30 + "!", encoding="utf8")
        self.tools.context.timeout = .2
        scope = TaskCleanup()
        with cleanup_scope(scope):
            result = self.tools.execute("search_text", {"query": "(a+)+$", "use_regex": True})
        self.assertFalse(result.ok)
        self.assertEqual("timeout", result.error.code.value)
        self.assertEqual("replan", result.error.recovery.value)
        self.assertFalse(scope.has_pending)
        self.assertFalse(scope.failed)
        self.assertIsNone(scope.started_deadline)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tools = ToolRegistry(ToolContext(WorkspacePolicy(self.root), CommandPolicy(), approver=lambda *_: True, timeout=1))

    def test_catastrophic_regex_returns_product_timeout_not_outer_kill(self):
        (self.root / "input.txt").write_text("a" * 30 + "!", encoding="utf8")
        code = '''import sys,json
from pathlib import Path
from tricoder.tools import ToolRegistry,ToolContext
from tricoder.policy import WorkspacePolicy,CommandPolicy
from tricoder.task_cleanup import TaskCleanup,cleanup_scope
t=ToolRegistry(ToolContext(WorkspacePolicy(Path(sys.argv[1])),CommandPolicy(),approver=lambda *_: True,timeout=.2))
s=TaskCleanup()
with cleanup_scope(s):
 r=t.execute('search_text',{'query':'(a+)+$','use_regex':True})
 print(json.dumps([r.ok,r.error.code.value,s.has_pending]))
 r=t.execute('search_text',{'query':'aaa','use_regex':True})
 print(json.dumps([r.ok,s.has_pending]))
'''
        result = subprocess.run([sys.executable, "-c", code, str(self.root)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual([False, "timeout", False], json.loads(lines[0]))
        self.assertEqual([True, False], json.loads(lines[1]))

    def test_scan_entry_limit_is_not_successful_no_match(self):
        for i in range(6):
            (self.root / f"{i}.txt").write_text("hello", encoding="utf8")
        with patch("tricoder.tools.search._MAX_SEARCH_ENTRIES", 4, create=True):
            result = self.tools.execute("search_text", {"query": "missing"})
        self.assertFalse(result.ok)

    def test_text_byte_limit_is_not_successful_no_match(self):
        (self.root / "data.txt").write_text("hello" * 5, encoding="utf8")
        with patch("tricoder.tools.search._MAX_SEARCH_BYTES", 10, create=True):
            result = self.tools.execute("search_text", {"query": "missing"})
        self.assertFalse(result.ok)

    def test_worker_ignores_workspace_python_modules_and_handles_chinese(self):
        (self.root / "re.py").write_text("raise RuntimeError('workspace module loaded')", encoding="utf8")
        (self.root / "regex_worker.py").write_text("raise RuntimeError('shadow')", encoding="utf8")
        (self.root / "data.txt").write_text("你好世界\n你好同学", encoding="utf8")
        result = self.tools.execute("search_text", {"query": "你好.*", "use_regex": True})
        self.assertTrue(result.ok, result)
        self.assertIn("data.txt:1: 你好世界", result.output)

    def test_bounded_stdin_roundtrip_and_blocked_writer(self):
        result = run_bounded_process([sys.executable, "-I", "-S", "-c", "import sys;print(len(sys.stdin.buffer.read()))"],
            cwd=self.root, env=dict(os.environ), timeout=2, max_output_bytes=100,
            stdin_bytes=b"x" * 100000)
        self.assertEqual("100000", result.stdout.strip())
        scope = TaskCleanup()
        with cleanup_scope(scope):
            result = run_bounded_process([sys.executable, "-I", "-S", "-c", "import time;time.sleep(5)"],
                cwd=self.root, env=dict(os.environ), timeout=.1, max_output_bytes=100,
                stdin_bytes=b"x" * 1000000)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.cleanup_failed)
        self.assertFalse(scope.has_pending)

    def test_stdin_limit_checked_before_process_start(self):
        with patch("tricoder.process.control.subprocess.Popen") as start:
            with self.assertRaises(ValueError):
                run_bounded_process([sys.executable], cwd=self.root, env={}, timeout=1,
                                    max_output_bytes=100, stdin_bytes=b"x" * (32*1024*1024 + 1))
            start.assert_not_called()

    def test_worker_invalid_output_and_cleanup_failure(self):
        (self.root / "data.txt").write_text("hello", encoding="utf8")
        for process_result in (
            BoundedProcessResult(0, "{", ""),
            BoundedProcessResult(0, '{"matches":null,"truncated":false}', ""),
            BoundedProcessResult(0, "", "", output_exceeded=True),
            BoundedProcessResult(0, "", "", input_failed=True),
            BoundedProcessResult(0, "", "", cleanup_failed=True),
        ):
            with self.subTest(process_result=process_result), patch("tricoder.tools.search.run_bounded_process", return_value=process_result):
                result = self.tools.execute("search_text", {"query": "hello", "use_regex": True})
                self.assertFalse(result.ok)
                if process_result.cleanup_failed:
                    self.assertEqual("cleanup_failed", result.error.code.value)
                    self.assertEqual("stop_task", result.error.recovery.value)

    def test_normal_limits_ignore_and_no_match(self):
        (self.root / ".gitignore").write_text("ignored/\n", encoding="utf8")
        (self.root / "ignored").mkdir()
        (self.root / "ignored/data.txt").write_text("secret", encoding="utf8")
        (self.root / "data.txt").write_text("hello\n"*210, encoding="utf8")
        for regex in (False, True):
            result = self.tools.execute("search_text", {"query": "hello", "use_regex": regex})
            self.assertTrue(result.ok, result)
            self.assertIn("截断", result.output)
            result = self.tools.execute("search_text", {"query": "secret", "use_regex": regex})
            self.assertTrue(result.ok)
            self.assertEqual("(无匹配)", result.output)
        self.assertFalse(self.tools.execute("search_text", {"query": "[", "use_regex": True}).ok)

    def test_expired_scan_budget_does_not_start_worker(self):
        self.tools.context.timeout = 0
        with patch("tricoder.tools.search.run_bounded_process") as start:
            result = self.tools.execute("search_text", {"query": "hello", "use_regex": True})
        self.assertFalse(result.ok)
        self.assertEqual("timeout", result.error.code.value)
        start.assert_not_called()

    def test_stdin_early_exit_and_cancellation_have_no_live_writer(self):
        scope = TaskCleanup()
        with cleanup_scope(scope):
            result = run_bounded_process([sys.executable, "-I", "-S", "-c", "pass"],
                cwd=self.root, env={}, timeout=2, max_output_bytes=100, stdin_bytes=b"x"*1000000)
        self.assertTrue(result.input_failed)
        self.assertFalse(scope.has_pending)

    def test_synchronous_search_passes_cancellation_to_matching_process(self):
        (self.root / "data.txt").write_text("a"*30+"!", encoding="utf8")
        token = CancellationToken()
        timer = threading.Timer(.1, token.cancel)
        scope = TaskCleanup()
        timer.start()
        try:
            with cleanup_scope(scope), self.assertRaises(CancellationError):
                self.tools.execute("search_text", {"query": "(a+)+$", "use_regex": True}, cancellation=token)
        finally:
            timer.join()
        self.assertFalse(scope.has_pending)
        token = CancellationToken()
        timer = threading.Timer(.15, token.cancel)
        timer.start()
        try:
            with cleanup_scope(scope), self.assertRaises(CancellationError):
                run_bounded_process([sys.executable, "-I", "-S", "-c", "import time;time.sleep(5)"],
                    cwd=self.root, env={}, timeout=3, max_output_bytes=100,
                    stdin_bytes=b"x"*1000000, cancellation=token)
        finally:
            timer.join()
        self.assertFalse(scope.has_pending)
