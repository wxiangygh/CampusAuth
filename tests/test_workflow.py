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
