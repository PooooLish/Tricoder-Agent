"""Host budget schedule, actual prepared requests, usage and turn boundaries."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from delivery_helpers import DeliveryHarness, QueueProvider, call, finish
from tricoder.agent import CodingAgent
from tricoder.core.cancellation import CancellationToken
from tricoder.models import MemoryConfig, ProviderResponse, TokenUsage, SessionContext
from tricoder.context.summarizer import SUMMARY_SYSTEM_PROMPT


def budgets(messages):
    result=[]
    for m in messages:
        if m.kind == 'protocol_feedback' and m.content.startswith('{'):
            data=json.loads(m.content)
            if data.get('type') == 'execution_budget':
                result.append((m,data))
    return result


class BudgetFeedbackTests(unittest.TestCase):
    def test_pure_schedule_and_invalid_inputs(self):
        from tricoder.engine.budget_feedback import build_round_budget_feedback as build
        for maximum in (1,2,3,4,16):
            seen=[]
            for n in range(1,maximum+1):
                value=build(round_number=n,max_rounds=maximum)
                if value is None: continue
                data=json.loads(value); seen.append(n)
                self.assertEqual('execution_budget',data['type'])
                self.assertEqual(n,data['round_number'])
                self.assertEqual(maximum,data['max_rounds'])
                self.assertEqual(maximum-n,data['remaining_after_current'])
                self.assertEqual(maximum-n+1,data['remaining_including_current'])
                self.assertLessEqual(len(value),800)
            self.assertEqual(sorted({1,*range(max(1,maximum-2),maximum+1)}),seen)
        for n,maximum in ((True,16),(1,False),(1.0,16),(1,16.0),(0,16),(17,16),(1,0),(-1,3)):
            with self.subTest(n=n,maximum=maximum), self.assertRaises(ValueError):
                build(round_number=n,max_rounds=maximum)

    def test_prepare_provider_prefix_usage_protocol_and_memory_matrix(self):
        for protocol in ('native','legacy_json'):
            for memory in ('off','structured'):
                actions=[[call('read_file',path=f'f{i}.py')] for i in range(15)]+[finish()]
                with self.subTest(protocol=protocol,memory=memory), DeliveryHarness(actions,protocol=protocol) as h:
                    for i in range(15): (h.workspace/f'f{i}.py').write_text(f'value={i}\n')
                    # Same real tools; capture the actual prepare result used by the Agent.
                    from tricoder.tools import ToolContext, ToolRegistry
                    from tricoder.policy import CommandPolicy, WorkspacePolicy
                    registry=ToolRegistry(ToolContext(WorkspacePolicy(h.workspace),CommandPolicy(),lambda *_:True))
                    agent=CodingAgent(h.provider,registry,tool_protocol=protocol,max_rounds=16,
                        max_context_chars=100000,plan_enabled=False,
                        memory_config=MemoryConfig(compaction=memory,persistence='off'))
                    original=h.provider.complete
                    def complete(messages,tools=()):
                        return replace(original(messages,tools),usage=TokenUsage(input_tokens=100,output_tokens=10))
                    snapshots=[]; prepare=agent.context_manager.prepare
                    def capture(messages):
                        snap=prepare(messages); snapshots.append(snap); return snap
                    with patch.object(h.provider,'complete',side_effect=complete), patch.object(agent.context_manager,'prepare',side_effect=capture):
                        turn=agent.run_with_context('read then finish', SessionContext())
                    self.assertTrue(turn.result.ok,turn.result)
                    self.assertEqual(16,len(h.provider.histories))
                    last=budgets(h.provider.histories[-1])
                    self.assertEqual([1,14,15,16],[d['round_number'] for _,d in last])
                    self.assertEqual(4,len(budgets(turn.context.messages)))
                    for m,_ in last:
                        self.assertIsNotNone(m.message_seq); self.assertIsNotNone(m.task_id)
                    for index in (1,13,14):
                        previous=h.provider.histories[index-1]; current=h.provider.histories[index]
                        self.assertEqual(previous,current[:len(previous)])
                    actual=[s for s in snapshots if list(s.messages) in h.provider.histories]
                    self.assertTrue(any(s.token_source=='provider' for s in actual))
                    if protocol=='native':
                        history=h.provider.histories[-1]
                        self.assertEqual({c.id for m in history for c in m.tool_calls},
                                         {m.tool_call_id for m in history if m.role=='tool'})
                    h.provider.actions=[finish()]
                    next_turn=agent.run_with_context('new confirmation',turn.context)
                    self.assertTrue(next_turn.result.ok,next_turn.result)
                    current_task=next(m.task_id for m in reversed(next_turn.context.messages) if m.kind=='task')
                    current=[d for m,d in budgets(next_turn.context.messages) if m.task_id==current_task]
                    self.assertEqual([1],[d['round_number'] for d in current])
                    sent=budgets(h.provider.histories[-1])
                    self.assertEqual([1],[d['round_number'] for _,d in sent])
                    self.assertTrue(all(m.task_id==current_task for m,_ in sent))
                    self.assertIn('不得整理成目标、约束、决定或待办',SUMMARY_SYSTEM_PROMPT)

    def test_cancelled_before_round_has_no_budget_or_request(self):
        from tricoder.tools import ToolContext, ToolRegistry
        from tricoder.policy import CommandPolicy, WorkspacePolicy
        with DeliveryHarness([]) as h:
            agent=CodingAgent(h.provider,ToolRegistry(ToolContext(WorkspacePolicy(h.workspace),CommandPolicy(),lambda *_:True)),plan_enabled=False)
            token=CancellationToken(); token.cancel()
            turn=agent.run_with_context('cancel',SessionContext(),cancellation=token)
            self.assertFalse(turn.result.ok)
            self.assertEqual([],h.provider.histories)
            self.assertEqual([],budgets(turn.context.messages))

    def test_small_round_budgets_do_not_auto_finish(self):
        for maximum in (1,2,3):
            actions=[[call('read_file',path=f'f{i}.py')] for i in range(maximum)]
            with self.subTest(maximum=maximum), DeliveryHarness(actions,max_rounds=maximum) as h:
                for i in range(maximum): (h.workspace/f'f{i}.py').write_text(str(i))
                result=h.run('read without finish')
                self.assertFalse(result.ok)
                self.assertEqual('round_budget_exhausted',result.stop_reason)
                self.assertEqual(maximum,len(h.provider.histories))
                self.assertEqual(maximum,len(budgets(h.provider.histories[-1])))

    def test_budget_feedback_does_not_increment_missing_tool_counter(self):
        from tricoder.tools import ToolContext, ToolRegistry
        from tricoder.policy import CommandPolicy, WorkspacePolicy
        from test_agent_termination import StrictQueueProvider
        with DeliveryHarness([]) as h:
            p=StrictQueueProvider([ProviderResponse(content='no tool')]*3)
            agent=CodingAgent(p,ToolRegistry(ToolContext(WorkspacePolicy(h.workspace),CommandPolicy(),lambda *_:True)),max_rounds=3,plan_enabled=False,
                             memory_config=MemoryConfig(compaction='off',persistence='off'))
            result=agent.run('task')
            self.assertFalse(result.ok)
            self.assertEqual(3,len(p.histories))
            self.assertEqual(3,len(budgets(p.histories[-1])))
            self.assertIn('累计 3 次',result.summary)


class BudgetSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_receives_host_kind_and_next_turn_resets_budget(self):
        from tricoder.context.summarizer import MemorySummarizer
        from tricoder.context.memory import ConversationMemory
        from tricoder.core.events import TextDelta, ProviderCompleted
        from tricoder.models import Message
        from tricoder.engine.budget_feedback import build_round_budget_feedback
        from test_memory_summarizer import StreamProvider
        provider=StreamProvider((TextDelta(json.dumps({'goal':None,'constraints':[], 'decisions':[], 'open_items':[]})),ProviderCompleted('stop')))
        summarizer=MemorySummarizer(provider,MemoryConfig(compaction='structured',persistence='off'))
        messages=(Message('user','old task',kind='task',message_seq=1,task_id='task-1'),
                  Message('user',build_round_budget_feedback(round_number=16,max_rounds=16),kind='protocol_feedback',message_seq=2,task_id='task-1'))
        result=await summarizer.summarize(ConversationMemory(),messages,CancellationToken())
        request=provider.calls[0][0]
        self.assertIn('不得整理成目标、约束、决定或待办',request[0].content)
        source=json.loads(request[1].content)['history'][1]
        self.assertEqual('protocol_feedback',source['kind'])
        self.assertEqual('execution_budget',json.loads(source['content'])['type'])
        self.assertIsNone(result.candidate.goal)
        self.assertEqual((),result.candidate.constraints)
        # The summarized previous turn is not a source of action-round counters.
        with DeliveryHarness([finish()]) as h:
            from tricoder.tools import ToolContext,ToolRegistry
            from tricoder.policy import WorkspacePolicy,CommandPolicy
            agent=CodingAgent(h.provider,ToolRegistry(ToolContext(WorkspacePolicy(h.workspace),CommandPolicy(),lambda *_:True)),plan_enabled=False,
                memory_config=MemoryConfig(compaction='structured',persistence='off'))
            turn=await agent.run_with_context_async('new task',SessionContext(conversation_memory=result.candidate))
            self.assertTrue(turn.result.ok,turn.result)
            self.assertEqual([1],[d['round_number'] for _,d in budgets(h.provider.histories[0])])
