"""A current trusted failure may receive one correction within the same Runtime task."""
import json
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from delivery_helpers import DeliveryHarness, call, check, edit, finish
from test_verification_delivery_state import CREATE_TEST, TARGET_TEST
from tricoder.engine.finalization import TaskFinalizer
from tricoder.session.lock import SessionLock, SessionLockBusyError
from tricoder.workspace.lock import WorkspaceLock, WorkspaceLockBusyError
from tricoder.workspace.verification import VerificationScope
from tricoder.task_cleanup import current_cleanup


def events(h):
    return [json.loads(line) for p in (h.root / 'audit').rglob('*.jsonl')
            for line in p.read_text(encoding='utf-8-sig').splitlines()]


class UnresolvedFinishRuntimeTests(unittest.TestCase):
    def test_failed_finish_corrects_then_exact_retest_same_task_and_owner(self):
        for protocol in ('native', 'legacy_json'):
            actions = [CREATE_TEST, TARGET_TEST, finish(), edit(), TARGET_TEST, check(), finish()]
            with self.subTest(protocol=protocol), DeliveryHarness(actions, protocol=protocol) as h:
                scopes, closed = [], []
                real_close = TaskFinalizer.close_task
                def close(finalizer, result):
                    closed.append(result.ok)
                    return real_close(finalizer, result)
                def before_response(index):
                    scopes.append(current_cleanup())
                    if index < 7:
                        self.assertEqual([], closed)
                    if index == 4:
                        self.assertEqual([], closed)
                        self.assertFalse(any(m.kind == 'task_termination' for m in h.provider.histories[-1]))
                        self.assertIsNone(current_cleanup().started_deadline)
                        with self.assertRaises(WorkspaceLockBusyError):
                            WorkspaceLock.acquire(h.workspace)
                        with self.assertRaises(SessionLockBusyError):
                            SessionLock(h.runtime.store.database_path, h.runtime.current.record.id)
                        if protocol == 'native':
                            start = time.monotonic()
                            while time.monotonic() - start < 5.2:
                                time.sleep(max(0.01, 5.2 - (time.monotonic() - start)))
                            self.assertGreaterEqual(time.monotonic() - start, 5.2)
                        self.assertIsNone(current_cleanup().started_deadline)
                h.provider.before_response = before_response
                with patch.object(TaskFinalizer, 'close_task', close):
                    result = h.run()
                self.assertTrue(result.ok, result)
                self.assertEqual(7, result.rounds)
                self.assertTrue(closed)
                self.assertTrue(all(closed))
                self.assertTrue(all(scope is scopes[0] for scope in scopes))
                self.assertIsNone(scopes[0].started_deadline)
                self.assertFalse(scopes[0].failed)
                self.assertFalse(scopes[0].has_pending)
                self.assertEqual((), result.pending_verification_paths)
                recovery = [event for event in events(h) if event.get('status') == 'finish_verification_recovery']
                self.assertEqual(1, len(recovery))
                self.assertEqual('unresolved_verification', recovery[0]['reason'])
                self.assertEqual((1, 1), (recovery[0]['count'], recovery[0]['limit']))
                feedback = next(f for f in h.provider.feedback() if f.get('recovery_count'))
                self.assertEqual('unresolved_verification', feedback['reason_code'])
                self.assertEqual(1, feedback['unresolved_check_count'])
                self.assertTrue(feedback['blocking_checks'])

    def test_successful_current_check_can_anchor_older_unresolved_failure(self):
        actions = [CREATE_TEST, TARGET_TEST, edit(), check('python -m compileall -q .'),
                   finish(), TARGET_TEST, finish()]
        with DeliveryHarness(actions) as h:
            result = h.run()
            self.assertTrue(result.ok, result)
            self.assertEqual(7, result.rounds)
            self.assertEqual((), result.task_validation.unresolved_check_ids)

    def test_recovery_does_not_accept_other_checks_or_a_second_finish(self):
        for middle in ([], check('python -m compileall -q .'),
                       check('python -m unittest -q'), check('python --version')):
            actions = [CREATE_TEST, TARGET_TEST, finish()] + ([middle] if middle else []) + [finish()]
            with self.subTest(middle=middle), DeliveryHarness(actions) as h:
                result = h.run()
                self.assertFalse(result.ok)
                self.assertEqual(len(actions), result.rounds)
                self.assertEqual('verification_blocked', result.stop_reason.value)
                self.assertEqual(1, len(result.task_validation.unresolved_check_ids))
                self.assertEqual(1, len([e for e in events(h) if e.get('status') == 'finish_verification_recovery']))

    def test_all_reasons_share_one_counter(self):
        cases = [
            [CREATE_TEST, finish(), TARGET_TEST, finish(), edit()],
            [CREATE_TEST, TARGET_TEST, finish(), edit(), TARGET_TEST, finish(), check()],
        ]
        for actions, rounds in zip(cases, (4, 6)):
            with self.subTest(rounds=rounds), DeliveryHarness(actions) as h:
                result = h.run()
                self.assertFalse(result.ok)
                self.assertEqual(rounds, result.rounds)
                self.assertEqual(1, len(h.provider.actions))
                self.assertEqual(1, len([e for e in events(h) if e.get('status') == 'finish_verification_recovery']))

    def test_external_change_stale_only_and_authority_have_no_recovery(self):
        for case in ('external', 'authority', 'stale_only', 'scan', 'deadline'):
            actions = [CREATE_TEST, TARGET_TEST]
            if case == 'stale_only':
                actions.append(edit())
            actions += [finish(), edit()]
            with self.subTest(case=case), DeliveryHarness(actions) as h:
                finish_round = len(actions) - 1
                original_capture = VerificationScope.capture
                at_finish = False
                deadline = []
                def before_response(index):
                    nonlocal at_finish
                    if index != finish_round:
                        return
                    at_finish = True
                    if case == 'external':
                        (h.workspace / 'external.py').write_text('x=1\n', encoding='utf8')
                    if case == 'authority':
                        h.runtime.current.tools.context.verification_scope._check_authority = object()
                    if case == 'deadline':
                        owner = current_cleanup()
                        deadline.append((owner, owner.begin_termination()))
                def capture(scope, policy):
                    result = original_capture(scope, policy)
                    return replace(result, complete=False) if at_finish and case == 'scan' else result
                h.provider.before_response = before_response
                with patch.object(VerificationScope, 'capture', capture):
                    result = h.run()
                self.assertFalse(result.ok)
                self.assertEqual(finish_round, result.rounds)
                self.assertEqual(1, len(h.provider.actions))
                self.assertFalse([e for e in events(h) if e.get('status') == 'finish_verification_recovery'])
                for owner, value in deadline:
                    self.assertEqual(value, owner.started_deadline)

    def test_explicit_incomplete_and_last_round_do_not_recover(self):
        for outcome, rounds in (('incomplete', 16), ('completed', 3)):
            with self.subTest(outcome=outcome), DeliveryHarness(
                [CREATE_TEST, TARGET_TEST, finish(outcome)], max_rounds=rounds) as h:
                result = h.run()
                self.assertFalse(result.ok)
                self.assertEqual(3, result.rounds)
                self.assertFalse([e for e in events(h) if e.get('status') == 'finish_verification_recovery'])

    def test_cancel_cleanup_unknown_and_legacy_do_not_recover(self):
        for case in ('cancelled', 'cleanup_failed', 'unknown_effects', 'legacy_unknown'):
            with self.subTest(case=case), DeliveryHarness([CREATE_TEST, TARGET_TEST, finish(), edit()]) as h:
                if case == 'legacy_unknown':
                    current = h.runtime.current
                    h.runtime.current = replace(current,
                        context=replace(current.context, verification_obligation='legacy_unknown'),
                        memory=replace(current.memory, verification_obligation='legacy_unknown'))
                def before_response(index):
                    if index != 3:
                        return
                    if case == 'cancelled':
                        h.runtime.cancel_current()
                    elif case == 'cleanup_failed':
                        current_cleanup().mark_failed()
                    elif case == 'unknown_effects':
                        h.runtime.current.tools.context.verification_scope.unknown_effects = True
                h.provider.before_response = before_response
                result = h.run()
                self.assertFalse(result.ok)
                self.assertEqual(3, len(h.provider.histories))
                self.assertFalse([e for e in events(h) if e.get('status') == 'finish_verification_recovery'])

    def test_recovery_audit_failure_and_approval_denial_stop(self):
        from tricoder.audit import AuditLogger
        original_log = AuditLogger.log
        for case in ('audit', 'approval', 'cleanup_after_audit', 'deadline_after_audit', 'unknown_after_audit'):
            with self.subTest(case=case), DeliveryHarness(
                    [CREATE_TEST, TARGET_TEST, finish(), TARGET_TEST, finish()]) as h:
                def log(logger, event):
                    if case == 'audit' and event.get('status') == 'finish_verification_recovery':
                        raise OSError('synthetic audit unavailable')
                    if event.get('status') == 'finish_verification_recovery':
                        if case == 'cleanup_after_audit':
                            current_cleanup().mark_failed()
                        elif case == 'deadline_after_audit':
                            current_cleanup().begin_termination()
                        elif case == 'unknown_after_audit':
                            h.runtime.current.tools.context.verification_scope.unknown_effects = True
                    return original_log(logger, event)
                def before_response(index):
                    if index == 4 and case == 'approval':
                        h.runtime.current.tools.context.approver = lambda *_: False
                h.provider.before_response = before_response
                with patch.object(AuditLogger, 'log', log):
                    result = h.run()
                self.assertFalse(result.ok)
                self.assertEqual(4 if case == 'approval' else 3, len(h.provider.histories))
                self.assertEqual(1, len(result.task_validation.unresolved_check_ids))
