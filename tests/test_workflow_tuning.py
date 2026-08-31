"""workflow_tuning 的算法单元测试。"""
import math
import tempfile
import unittest
from pathlib import Path

from core.workflow_tuning import (
    CHANGE_RATIO,
    MAX_RETRIES,
    MIN_SAMPLES_RETRIES,
    MIN_SAMPLES_TIMEOUT,
    WorkflowTuningStore,
    stability_score,
    suggest_retries_from_stats,
)


class StabilityScoreTests(unittest.TestCase):
    def test_perfect_step_is_full_score(self):
        self.assertEqual(stability_score(0, 0.0, 15.0), 100)

    def test_retries_weigh_more_than_time(self):
        # 同等"饱和度"下，重试扣分应重于耗时扣分（0.7 > 0.3）
        only_retries = stability_score(2.0, 0.0, 15.0)   # 扣 70 分
        only_time = stability_score(0.0, 15.0 * 0.8, 15.0)  # 扣 30 分
        self.assertGreater(100 - only_retries, 100 - only_time)

    def test_score_is_clamped_to_zero(self):
        self.assertEqual(stability_score(2.0, 12.0, 15.0), 0)

    def test_score_never_negative(self):
        score = stability_score(5.0, 999.0, 1.0)
        self.assertGreaterEqual(score, 0)


class SuggestRetriesTests(unittest.TestCase):
    def test_no_failure_needs_no_retry(self):
        self.assertEqual(suggest_retries_from_stats(0.0, 0.0), 0)

    def test_low_failure_rate_needs_one_retry(self):
        # 5% 失败率：ln(0.05)/ln(0.95) ≈ 58 → 远超上限，但 floor=0 → 结果受上限约束
        # 这里验证的是中低失败率的合理行为
        self.assertGreaterEqual(suggest_retries_from_stats(0.3, 0.0), 1)

    def test_observed_retries_floor_is_respected(self):
        # 观测到平均 2 次重试，即使最近窗口全成功也建议保留 2 次冗余
        self.assertEqual(suggest_retries_from_stats(0.0, 1.6), 2)

    def test_suggestion_is_capped(self):
        self.assertLessEqual(suggest_retries_from_stats(0.9, 5.0), MAX_RETRIES)

    def test_geometric_model_matches_formula(self):
        # 失败率 50%：ceil(ln 0.05 / ln 0.5) - 1 = ceil(4.32) - 1 = 4
        self.assertEqual(suggest_retries_from_stats(0.5, 0.0), 4)


class WorkflowTuningStoreTests(unittest.TestCase):
    def _store(self):
        temp = Path(tempfile.mkdtemp()) / 'tuning.json'
        return WorkflowTuningStore(temp)

    def test_record_accumulates_stats(self):
        store = self._store()
        store.record('wf', 'step', 2.0, 0, True, timeout=10.0)
        store.record('wf', 'step', 4.0, 1, True, timeout=10.0)
        stats = store.stats('wf', 'step')
        self.assertEqual(stats['runs'], 2)
        self.assertAlmostEqual(stats['avg_retries'], 0.5)
        self.assertIn('score', stats)

    def test_stats_for_unknown_step(self):
        store = self._store()
        self.assertEqual(store.stats('wf', 'step'), {'runs': 0})

    def test_ewma_converges_toward_recent_values(self):
        store = self._store()
        for _ in range(30):
            store.record('wf', 'step', 5.0, 0, True, timeout=15.0)
        # 稳定输入后 SRTT 应收敛到 5 附近
        stats = store.stats('wf', 'step')
        self.assertAlmostEqual(stats['avg_elapsed'], 5.0, delta=0.2)

    def test_timeout_suggestion_covers_observed_jitter(self):
        store = self._store()
        # 交替快慢样本制造高 RTTVAR，建议超时应明显大于平均耗时
        for i in range(20):
            store.record('wf', 'step', 1.0 if i % 2 else 9.0, 0, True, timeout=5.0)
        suggestion = store.suggest_timeout('wf', 'step', current=5.0)
        self.assertIsNotNone(suggestion)
        self.assertGreater(suggestion, 5.0 * (1 + CHANGE_RATIO))

    def test_stable_input_keeps_timeout_close_to_mean(self):
        store = self._store()
        for _ in range(20):
            store.record('wf', 'step', 2.0, 0, True, timeout=8.0)
        # 稳定输入下建议值应回落（差异显著才返回，此处应返回更贴近 3 左右的值）
        suggestion = store.suggest_timeout('wf', 'step', current=8.0)
        self.assertIsNotNone(suggestion)
        self.assertLess(suggestion, 8.0)

    def test_too_few_samples_gives_no_timeout_suggestion(self):
        store = self._store()
        for _ in range(MIN_SAMPLES_TIMEOUT - 1):
            store.record('wf', 'step', 2.0, 0, True, timeout=10.0)
        self.assertIsNone(store.suggest_timeout('wf', 'step', current=10.0))

    def test_too_few_runs_gives_no_retry_suggestion(self):
        store = self._store()
        for _ in range(MIN_SAMPLES_RETRIES - 1):
            store.record('wf', 'step', 2.0, 0, False, timeout=10.0)
        self.assertIsNone(store.suggest_retries('wf', 'step', current=0))

    def test_no_change_within_deadband(self):
        store = self._store()
        for _ in range(10):
            store.record('wf', 'step', 2.0, 0, True, timeout=10.0)
        # 与收敛值非常接近的当前值不应触发建议
        suggestion = store.suggest_timeout('wf', 'step', current=3.0)
        self.assertIsNone(suggestion)

    def test_persistence_roundtrip(self):
        path = Path(tempfile.mkdtemp()) / 'tuning.json'
        store = WorkflowTuningStore(path)
        store.record('wf', 'step', 3.0, 1, True, timeout=9.0)
        reloaded = WorkflowTuningStore(path)
        stats = reloaded.stats('wf', 'step')
        self.assertEqual(stats['runs'], 1)
        self.assertEqual(stats['avg_retries'], 1)

    def test_recent_window_is_bounded(self):
        store = self._store()
        for _ in range(50):
            store.record('wf', 'step', 1.0, 0, True, timeout=5.0)
        state = store._state('wf', 'step')
        self.assertLessEqual(len(state['recent']), 20)

    def test_corrupt_file_starts_empty(self):
        path = Path(tempfile.mkdtemp()) / 'tuning.json'
        path.write_text('{ not valid json', encoding='utf-8')
        store = WorkflowTuningStore(path)
        self.assertEqual(store.stats('wf', 'step'), {'runs': 0})

    def test_apply_to_workflow_adjusts_steps(self):
        store = self._store()
        for _ in range(20):
            store.record('wf', 'step_a', 1.0, 0, True, timeout=30.0)
        steps = [{'id': 'step_a', 'enabled': True, 'retries': 0,
                  'timeout': 30.0, 'retry_delay': 1.0, 'continue_on_error': False}]
        changed = store.apply_to_workflow('wf', steps)
        self.assertTrue(changed)
        self.assertLess(steps[0]['timeout'], 30.0)

    def test_apply_to_workflow_without_samples_keeps_steps(self):
        store = self._store()
        steps = [{'id': 'step_a', 'enabled': True, 'retries': 0,
                  'timeout': 15.0, 'retry_delay': 1.0, 'continue_on_error': False}]
        self.assertFalse(store.apply_to_workflow('wf', steps))
        self.assertEqual(steps[0]['timeout'], 15.0)


class RecordStepStatsTests(unittest.TestCase):
    """auth_workflow._record_step_stats 的样本入库规则。"""

    def _result(self, code, step_stats):
        from core.workflow import WorkflowResult
        return WorkflowResult(code == 'ok', '', code, None, 1.0, (),
                              step_stats=step_stats)

    def test_cancelled_run_records_nothing(self):
        from core.auth_workflow import _record_step_stats
        from core.workflow_tuning import configure_tuning
        store = configure_tuning(Path(tempfile.mkdtemp()) / 'tuning.json')
        result = self._result('cancelled', {
            'a': {'elapsed': 2.0, 'retries': 1, 'success': False,
                  'timeout': 10.0, 'executed': True}})
        _record_step_stats('wf', result, {})
        self.assertEqual(store.workflow_stats('wf'), {})

    def test_unexecuted_placeholders_are_skipped(self):
        from core.auth_workflow import _record_step_stats
        from core.workflow_tuning import configure_tuning
        store = configure_tuning(Path(tempfile.mkdtemp()) / 'tuning.json')
        result = self._result('workflow_timeout', {
            'a': {'elapsed': 2.0, 'retries': 0, 'success': True,
                  'timeout': 10.0, 'executed': True},
            'b': {'elapsed': 0.0, 'retries': 0, 'success': False,
                  'timeout': 10.0, 'executed': False}})
        _record_step_stats('wf', result, {})
        stats = store.workflow_stats('wf')
        self.assertIn('a', stats)
        self.assertNotIn('b', stats)


class ApplyAutoTuneTests(unittest.TestCase):
    """auth_workflow.apply_auto_tune 的一键应用契约（运行后自动应用共用）。"""

    def setUp(self):
        temp = Path(tempfile.mkdtemp())
        from core.config import configure_config
        from core.workflow_tuning import configure_tuning
        self.tuning = configure_tuning(temp / 'tuning.json')
        self.config = configure_config(temp / 'config.json')
        # built_in 由配置规范化的 fallback 决定，必须用真实内置工作流测试
        wf = self.config.snapshot()['workflows']['default_auth']
        self.step_id = wf['steps'][0]['id']
        self.old_timeout = float(wf['steps'][0]['timeout'])
        self.old_retries = int(wf['steps'][0]['retries'])

    def test_returns_change_summary_and_persists(self):
        from core.auth_workflow import apply_auto_tune
        # 3 次快速成功 → 超时建议显著小于默认值；再补 2 次失败（失败率 2/5）→ 重试建议增大
        for _ in range(3):
            self.tuning.record('default_auth', self.step_id, 1.0, 0, True,
                               timeout=self.old_timeout)
        for _ in range(2):
            self.tuning.record('default_auth', self.step_id, 1.0, 0, False,
                               timeout=self.old_timeout)
        changes = apply_auto_tune('default_auth')
        self.assertEqual(len(changes), 1)
        c = changes[0]
        self.assertEqual(c['id'], self.step_id)
        self.assertEqual(c['timeout_from'], self.old_timeout)
        self.assertLess(c['timeout'], self.old_timeout)
        self.assertEqual(c['retries_from'], self.old_retries)
        self.assertGreaterEqual(c['retries'], 1)
        saved = self.config.snapshot()['workflows']['default_auth']
        step = next(s for s in saved['steps'] if s['id'] == self.step_id)
        self.assertEqual(step['timeout'], c['timeout'])
        self.assertEqual(step['retries'], c['retries'])
        self.assertTrue(saved.get('customized'))  # 内置工作流被调整后需标记以持久化

    def test_without_samples_returns_empty_and_keeps_config(self):
        from core.auth_workflow import apply_auto_tune
        before = self.config.snapshot()['workflows']['default_auth']
        self.assertEqual(apply_auto_tune('default_auth'), [])
        self.assertEqual(self.config.snapshot()['workflows']['default_auth'], before)

    def test_unknown_workflow_returns_empty(self):
        from core.auth_workflow import apply_auto_tune
        self.assertEqual(apply_auto_tune('not_exist'), [])


if __name__ == '__main__':
    unittest.main()
