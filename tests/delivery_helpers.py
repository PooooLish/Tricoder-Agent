"""Offline delivery fixtures with real Runtime, tools and project-local fresh DBs."""
import json
from pathlib import Path
import tempfile

from tricoder.models import AppConfig, MemoryConfig, ProviderConfig, ProviderResponse, ToolCall
from tricoder.session.runtime import RuntimeOptions, SessionRuntime
from tricoder.session.store import SessionStore

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = "def select(initial, latest):\n    return initial\n"
FIXED = "def select(initial, latest):\n    return latest\n"
TESTS = "import unittest\nfrom config import select\nclass TestSelect(unittest.TestCase):\n    def test_latest(self):\n        self.assertEqual('', select('red', ''))\n"


def call(name, **arguments):
    return name, arguments


def finish(outcome="completed"):
    return [call("finish", summary="synthetic delivery", outcome=outcome)]


def edit():
    return [call("edit_file", path="config.py", old_text=ORIGINAL, new_text=FIXED)]


def check(command="python -m compileall -q config.py"):
    return [call("run_command", command=command)]


def reconstructed():
    return [
        edit() + [call("create_directory", path="tests", parents=True)],
        [call("create_file", path="tests/test_config.py", content=TESTS)],
        check("python -m unittest -q"),
        check("python -m unittest discover -s tests -t . -v"),
        [call("create_file", path="tests/__init__.py", content="# synthetic package\n")],
        check("python -m unittest -q"),
    ]


class QueueProvider:
    def __init__(self, actions, protocol="native"):
        self.actions = list(actions)
        self.histories = []
        self.protocol = protocol
        self.before_response = None

    def complete(self, messages, tools=()):
        self.histories.append(list(messages))
        if self.before_response:
            self.before_response(len(self.histories))
        if not self.actions:
            raise AssertionError("unexpected provider request")
        batch = self.actions.pop(0)
        if self.protocol == "legacy_json":
            assert len(batch) == 1
            name, arguments = batch[0]
            return ProviderResponse(content=json.dumps({"tool": name, "arguments": arguments, "reason": "synthetic"}))
        return ProviderResponse(tool_calls=tuple(
            ToolCall(f"{len(self.histories)}-{i}", name, arguments)
            for i, (name, arguments) in enumerate(batch)), finish_reason="tool_calls")

    def feedback(self):
        return [json.loads(m.content) for m in self.histories[-1]
                if m.kind == "verification_feedback"]


class DeliveryHarness:
    def __init__(self, actions, *, max_rounds=16, protocol="native", confirmer=True):
        self.temp = tempfile.TemporaryDirectory(prefix="delivery-test-", dir=ROOT / "runtime")
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "config.py").write_text(ORIGINAL, encoding="utf8")
        self.provider = QueueProvider(actions, protocol)
        self.approvals = []
        self.confirmations = []
        store = SessionStore(self.root / "synthetic.db")
        store.initialize(self.workspace)
        session = store.create("delivery", self.workspace, "deepseek", "offline")
        config = AppConfig(workspace=self.workspace, provider=ProviderConfig(
            "deepseek", "synthetic", "https://example.invalid", "offline"),
            audit_dir=self.root / "audit", plan_enabled=False, max_rounds=max_rounds,
            tool_protocol=protocol, memory=MemoryConfig(compaction="off", persistence="off"))

        def confirm(notice):
            self.confirmations.append(notice)
            return confirmer

        self.runtime = SessionRuntime(store, self.workspace, options=RuntimeOptions(environ={}),
            initial_session_id=session.id, config_loader=lambda **_: config,
            provider_factory=lambda *_: self.provider,
            approver=lambda *args: self.approvals.append(args) or True,
            workspace_confirmer=confirm)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.runtime.close()
        self.temp.cleanup()

    def run(self, task="修复 config.py 并验证"):
        return self.runtime.run_task(task)
