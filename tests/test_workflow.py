import unittest

from core.workflow import StepResult, WorkflowContext, WorkflowRunner


CATALOG = {'first': {'name': 'First'}, 'second': {'name': 'Second'},
           'finalize': {'name': 'Finalize'}}


class WorkflowTests(unittest.TestCase):
    def test_retry_then_success_and_event_order(self):
        calls = []
        events = []

        def first(context, step):
            calls.append(context.current_attempt)
            return StepResult.ok('ready') if context.current_attempt == 2 else StepResult.fail(
                'temporary', retryable=True)

        runner = WorkflowRunner({'first': first, 'finalize': lambda c, s: StepResult.ok('done')}, CATALOG)
        context = WorkflowContext(config={}, cancelled=lambda: False, publish=events.append)
        result = runner.run([
            {'id': 'first', 'retries': 1, 'retry_delay': 0, 'timeout': 2},
            {'id': 'finalize', 'timeout': 2},
        ], context)
        self.assertTrue(result.success)
        self.assertEqual(calls, [1, 2])
        self.assertIn('retrying', [event['status'] for event in events])

    def test_failure_runs_rollbacks_in_reverse_order(self):
        rolled_back = []

        def first(context, step):
            context.add_rollback('one', lambda: rolled_back.append('one'))
            context.add_rollback('two', lambda: rolled_back.append('two'))
            return StepResult.ok('ready')

        runner = WorkflowRunner({
            'first': first,
            'finalize': lambda c, s: StepResult.fail('failed'),
        }, CATALOG)
        result = runner.run([{'id': 'first'}, {'id': 'finalize'}],
                            WorkflowContext(config={}, cancelled=lambda: False))
        self.assertFalse(result.success)
        self.assertEqual(rolled_back, ['two', 'one'])

    def test_custom_workflow_need_not_end_with_finalize(self):
        runner = WorkflowRunner({
            'first': lambda c, s: StepResult.ok('ok'),
            'second': lambda c, s: StepResult.ok('done'),
        }, CATALOG)
        steps = runner.validate([{'id': 'first'}, {'id': 'second'}])
        self.assertEqual([step.id for step in steps], ['first', 'second'])

    def test_same_action_can_be_repeated_for_fine_grained_flows(self):
        calls = []

        def first(context, step):
            calls.append(step.id)
            return StepResult.ok('ok')

        runner = WorkflowRunner({'first': first}, CATALOG)
        result = runner.run([{'id': 'first'}, {'id': 'first'}],
                            WorkflowContext(config={}, cancelled=lambda: False))
        self.assertTrue(result.success)
        self.assertEqual(calls, ['first', 'first'])


if __name__ == '__main__':
    unittest.main()


class StepStatsTests(unittest.TestCase):
    """runner 收集的节点耗时/重试数据（自动调优的数据源）。"""

    def test_stats_record_elapsed_retries_and_timeout(self):
        def flaky(context, step):
            if context.current_attempt == 1:
                return StepResult.fail('temporary', retryable=True)
            return StepResult.ok('ok')

        runner = WorkflowRunner({
            'first': flaky,
            'finalize': lambda c, s: StepResult.ok('done'),
        }, CATALOG)
        result = runner.run([
            {'id': 'first', 'retries': 1, 'retry_delay': 0, 'timeout': 7},
            {'id': 'finalize', 'timeout': 5},
        ], WorkflowContext(config={}, cancelled=lambda: False))
        self.assertTrue(result.success)
        self.assertEqual(result.step_stats['first']['retries'], 1)
        self.assertEqual(result.step_stats['first']['timeout'], 7)
        self.assertTrue(result.step_stats['first']['success'])
        self.assertTrue(result.step_stats['first']['executed'])
        self.assertGreaterEqual(result.step_stats['first']['elapsed'], 0.0)
        self.assertEqual(result.step_stats['finalize']['retries'], 0)

    def test_stats_present_on_failure(self):
        runner = WorkflowRunner({'first': lambda c, s: StepResult.fail('nope')}, CATALOG)
        result = runner.run([{'id': 'first', 'timeout': 3}],
                            WorkflowContext(config={}, cancelled=lambda: False))
        self.assertFalse(result.success)
        self.assertFalse(result.step_stats['first']['success'])

    def test_stats_present_on_cancel(self):
        runner = WorkflowRunner({'first': lambda c, s: StepResult.fail('nope')},
                                 CATALOG)
        result = runner.run([{'id': 'first'}],
                            WorkflowContext(config={}, cancelled=lambda: True))
        self.assertFalse(result.success)
        self.assertIn('first', result.step_stats)
        # 未真实执行的占位记录：供前端完整展示，但调优侧会跳过
        self.assertFalse(result.step_stats['first']['executed'])
        self.assertEqual(result.step_stats['first']['elapsed'], 0.0)

    def test_overall_timeout_leaves_placeholder_for_pending_step(self):
        import time as _time

        def slow(context, step):
            _time.sleep(0.05)
            return StepResult.ok('ok')

        runner = WorkflowRunner({'first': slow, 'second': slow}, CATALOG)
        context = WorkflowContext(config={}, cancelled=lambda: False)
        # 构造参数有 1 秒下限，直接覆写截止时间模拟总时限即将耗尽
        context.overall_deadline = _time.monotonic() + 0.02
        result = runner.run([{'id': 'first'}, {'id': 'second'}], context)
        self.assertEqual(result.code, 'workflow_timeout')
        self.assertTrue(result.step_stats['first']['executed'])
        self.assertFalse(result.step_stats['second']['executed'])
