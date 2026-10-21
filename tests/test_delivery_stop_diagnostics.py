"""Trusted terminal branches and redacted finish audit, using real Runtime."""
import json
import unittest
from unittest.mock import patch

from delivery_helpers import DeliveryHarness, call, check, edit, finish, reconstructed
from tricoder.engine.telemetry import audit_arguments
from tricoder.models import RunResult, ToolAction, ToolResult
from tricoder.providers import ProviderError


def events(h):
    return [json.loads(line) for p in (h.root / 'audit').rglob('*.jsonl')
            for line in p.read_text(encoding='utf8').splitlines()]


class DeliveryStopTests(unittest.TestCase):
    def reason(self, result, expected):
        self.assertEqual(expected, getattr(getattr(result, 'stop_reason', None), 'value', None))

    def test_finish_declaration_is_not_acceptance(self):
        for declared, ok, reason in [('completed', True, 'completed'),
                                     ('incomplete', False, 'model_incomplete')]:
            with self.subTest(declared=declared), DeliveryHarness([finish(declared)]) as h:
                result = h.run('只确认，不修改')
                self.assertEqual(ok, result.ok)
                self.reason(result, reason)
                rows = events(h)
                assessed = [e for e in rows if e.get('status') == 'finish_assessment']
                self.assertEqual(1, len(assessed))
                self.assertEqual(declared, assessed[0]['declared_outcome'])
                self.assertEqual(ok, assessed[0]['accepted'])
                self.assertEqual(reason, assessed[0]['reason'])
                self.assertNotIn('synthetic delivery', json.dumps(rows))

    def test_finish_argument_redaction_and_default(self):
        for value in ('PRIVATE_SECRET', [], {}, None, 3):
            args = audit_arguments(None, ToolAction('finish', {'summary': 'PRIVATE_SECRET', 'outcome': value}, ''), ToolResult(True, ''))
            self.assertNotIn('PRIVATE_SECRET', json.dumps(args))
            self.assertNotIn('outcome', args)
        args = audit_arguments(None, ToolAction('finish', {'summary': 'same'}, ''), ToolResult(True, ''))
        self.assertEqual({'summary_chars': 4, 'outcome': 'completed'}, args)

    def test_equal_rounds_budget_and_clarification_differ(self):
        reads = [[call('search_text', path='.', query=f'unique_{i}')] for i in range(1, 9)]
        for actions, expected in [(reads, 'round_budget_exhausted'),
                                  (reads[:7] + [[call('ask_user', question='more?')]], 'needs_input')]:
            with self.subTest(expected=expected), DeliveryHarness(actions, max_rounds=8) as h:
                result = h.run()
                self.assertFalse(result.ok)
                self.assertEqual(8, result.rounds)
                self.reason(result, expected)

    def test_seventh_incomplete_and_failed_completed_at_budget(self):
        for ending, expected in [('incomplete', 'model_incomplete'), ('completed', 'verification_blocked')]:
            with DeliveryHarness(reconstructed() + [finish(ending)], max_rounds=7) as h:
                r = h.run()
                self.assertFalse(r.ok)
                self.assertEqual(7, r.rounds)
                self.reason(r, expected)
                assessment = [e for e in events(h) if e.get('status') == 'finish_assessment'][-1]
                self.assertFalse(assessment['accepted'])
                self.assertEqual('failed', assessment['validation_status'])
                self.assertEqual(1, assessment['unresolved_check_count'])

    def test_provider_failure_and_runtime_rejection(self):
        with DeliveryHarness([finish()]) as h:
            def fail(_): raise ProviderError('PRIVATE_SECRET')
            h.provider.before_response = fail
            r = h.run()
            self.reason(r, 'provider_failed')
            self.assertNotIn('PRIVATE_SECRET', json.dumps(events(h)))
        with DeliveryHarness([finish()]) as h:
            h.provider.before_response = lambda _: (h.workspace / 'external.txt').write_text('external')
            r = h.run()
            self.assertFalse(r.ok)
            self.reason(r, 'runtime_rejected')

    def test_cancellation_cleanup_and_audit_failure_do_not_continue(self):
        from tricoder.task_cleanup import current_cleanup
        for kind in ('cancelled', 'cleanup_failed'):
            with DeliveryHarness([finish(), finish()]) as h:
                def stop(_):
                    if kind == 'cancelled': h.runtime.cancel_current()
                    else: current_cleanup().mark_failed()
                h.provider.before_response = stop
                r = h.run()
                self.assertFalse(r.ok)
                self.reason(r, kind)
                self.assertEqual(1, len(h.provider.histories))
        from tricoder.audit import AuditLogger
        original = AuditLogger.log
        def fail_assessment(logger, event):
            if event.get('status') == 'finish_assessment': raise OSError('synthetic')
            return original(logger, event)
        with DeliveryHarness([finish(), finish()]) as h, patch.object(AuditLogger, 'log', fail_assessment):
            r = h.run()
            self.assertFalse(r.ok)
            self.assertIsNone(r.stop_reason)
            self.assertEqual(1, len(h.provider.histories))

    def test_old_result_is_unknown(self):
        self.assertIsNone(RunResult(False, '达到最大轮数 8', 8).stop_reason)
        for value in ('PRIVATE', 'completed', [], 1):
            with self.assertRaises(ValueError):
                RunResult(True, '', 0, stop_reason=value)

    def test_progress_tool_stop_and_unknown_are_distinct(self):
        for actions, reason in [
            ([[call('read_file', path='config.py')]] * 4, 'progress_stopped'),
            ([[call('run_command', command='python -c pass')]], 'tool_stop'),
        ]:
            with DeliveryHarness(actions) as h:
                self.reason(h.run(), reason)
        from dataclasses import replace
        with DeliveryHarness([finish()]) as h:
            h.runtime.current = replace(h.runtime.current,
                context=replace(h.runtime.current.context, unknown_effects=True))
            r=h.run()
            self.assertFalse(r.ok)
            self.reason(r, 'unknown_effects')
            self.assertEqual(0,len(h.provider.histories))
