"""节点全局/独立双模式回归测试。

- resolve_global_nodes：global 节点运行时采用该类型的全局配置；
- 节点 params.link_mode：按节点显式指定有线/无线网卡（auto 沿用预扫描）；
- 自动调优对 global 节点写全局配置而非节点副本。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.auth_workflow import (_interface, apply_auto_tune,
                                resolve_global_nodes)
from core.config import configure_config
from core.workflow import StepSpec, WorkflowContext
from core.workflow_tuning import configure_tuning


class ResolveGlobalNodesTests(unittest.TestCase):
    STEPS = [
        {'id': 'enable_ipv4', 'enabled': True, 'timeout': 8, 'retries': 0,
         'retry_delay': 1, 'continue_on_error': False, 'node_mode': 'global'},
        {'id': 'enable_ipv4', 'enabled': True, 'timeout': 30, 'retries': 2,
         'retry_delay': 2, 'continue_on_error': True},
        {'id': 'portal_login', 'enabled': True, 'timeout': 8, 'retries': 1},
    ]

    def setUp(self):
        self.globals = {
            'enable_ipv4': {
                'config': {'timeout': 12, 'retries': 1, 'retry_delay': 1.5,
                           'continue_on_error': False,
                           'params': {'link_mode': 'wired'}},
                'source': {'workflow_id': 'wf_a', 'workflow_name': '工作流A'},
            },
        }

    def test_global_step_adopts_global_config(self):
        resolved = resolve_global_nodes(self.STEPS, self.globals)
        first = resolved[0]
        self.assertEqual(first['timeout'], 12)
        self.assertEqual(first['retries'], 1)
        self.assertEqual(first['retry_delay'], 1.5)
        self.assertEqual(first['continue_on_error'], False)
        self.assertEqual(first['params'], {'link_mode': 'wired'})
        # enabled 不参与全局共享，保留节点自身取值
        self.assertEqual(first['enabled'], True)

    def test_independent_step_keeps_own_config(self):
        resolved = resolve_global_nodes(self.STEPS, self.globals)
        second = resolved[1]
        self.assertEqual(second['timeout'], 30)
        self.assertEqual(second['retries'], 2)
        self.assertTrue(second['continue_on_error'])

    def test_missing_global_entry_falls_back_to_local(self):
        resolved = resolve_global_nodes(self.STEPS, {})
        self.assertEqual(resolved[0]['timeout'], 8)
        self.assertEqual(resolved[1]['timeout'], 30)

    def test_partial_global_config_only_overrides_present_keys(self):
        partial = {'enable_ipv4': {'config': {'timeout': 20}, 'source': {}}}
        resolved = resolve_global_nodes(self.STEPS, partial)
        self.assertEqual(resolved[0]['timeout'], 20)
        self.assertEqual(resolved[0]['retries'], 0)  # 全局未提供 → 保留自身


class NodeLinkModeTests(unittest.TestCase):
    """接口类节点 params.link_mode：显式有线/无线优先于工作流预扫描。"""

    def _context(self, link_type):
        context = WorkflowContext(config={}, cancelled=lambda: False)
        context.data['link_type'] = link_type
        return context

    def test_node_link_mode_wired_overrides_wireless_prescan(self):
        step = StepSpec(id='enable_ipv4', params={'link_mode': 'wired'})
        with patch('core.auth_workflow.get_wired_interface_name',
                   return_value='以太网') as wired, \
             patch('core.auth_workflow.get_wifi_interface_name') as wifi:
            name, error = _interface(self._context('wireless'), step)
        self.assertIsNone(error)
        self.assertEqual(name, '以太网')
        wired.assert_called_once()
        wifi.assert_not_called()

    def test_node_link_mode_wireless_overrides_wired_prescan(self):
        step = StepSpec(id='reset_ipv6_dns', params={'link_mode': 'wireless'})
        with patch('core.auth_workflow.get_wired_interface_name') as wired, \
             patch('core.auth_workflow.get_wifi_interface_name',
                   return_value='WLAN') as wifi:
            name, error = _interface(self._context('wired'), step)
        self.assertIsNone(error)
        self.assertEqual(name, 'WLAN')
        wifi.assert_called_once()
        wired.assert_not_called()

    def test_node_link_mode_auto_uses_prescan(self):
        step = StepSpec(id='enable_ipv4', params={'link_mode': 'auto'})
        with patch('core.auth_workflow.get_wired_interface_name',
                   return_value='以太网') as wired:
            name, error = _interface(self._context('wired'), step)
        self.assertIsNone(error)
        self.assertEqual(name, '以太网')
        wired.assert_called_once()

    def test_wired_mode_missing_adapter_fails(self):
        step = StepSpec(id='enable_ipv4', params={'link_mode': 'wired'})
        with patch('core.auth_workflow.get_wired_interface_name', return_value=None):
            name, error = _interface(self._context('wireless'), step)
        self.assertIsNone(name)
        self.assertEqual(error.code, 'interface_missing')


class ApplyAutoTuneGlobalTests(unittest.TestCase):
    """自动调优：global 节点的建议写入 node_globals，而非节点副本。"""

    def setUp(self):
        temp = Path(tempfile.mkdtemp())
        self.tuning = configure_tuning(temp / 'tuning.json')
        self.config = configure_config(temp / 'config.json')
        wf = self.config.snapshot()['workflows']['default_auth']
        # reset_ipv6_dns 在默认工作流中只出现一次，避免第二个独立副本干扰断言
        self.step_id = 'reset_ipv6_dns'
        step = next(s for s in wf['steps'] if s['id'] == self.step_id)
        # 把该节点标记为全局
        step['node_mode'] = 'global'
        workflows = self.config.snapshot()['workflows']
        # 内置工作流未 customized 时 steps 会被规范化重置，必须标记
        workflows['default_auth'] = {**wf, 'steps': wf['steps'], 'customized': True}
        self.config.patch({'workflows': workflows, 'node_globals': {}})

    def test_tune_writes_global_config_not_local_copy(self):
        from core.auth_workflow import resolve_global_nodes
        old_timeout = float(next(
            s for s in self.config.snapshot()['workflows']['default_auth']['steps']
            if s['id'] == self.step_id)['timeout'])
        for _ in range(3):
            self.tuning.record('default_auth', self.step_id, 1.0, 0, True,
                               timeout=old_timeout)
        changes = apply_auto_tune('default_auth')
        self.assertEqual(len(changes), 1)
        globals_map = self.config.snapshot()['node_globals']
        cfg = globals_map[self.step_id]['config']
        self.assertEqual(cfg['timeout'], changes[0]['timeout'])
        self.assertLess(cfg['timeout'], old_timeout)
        # 节点副本未被改动（运行时由全局配置覆盖）
        saved_step = next(
            s for s in self.config.snapshot()['workflows']['default_auth']['steps']
            if s['id'] == self.step_id)
        self.assertEqual(float(saved_step['timeout']), old_timeout)
        # 解析后 global 节点取到调优后的全局超时
        resolved = resolve_global_nodes(
            self.config.snapshot()['workflows']['default_auth']['steps'],
            globals_map)
        tuned = next(s for s in resolved if s['id'] == self.step_id)
        self.assertEqual(float(tuned['timeout']), cfg['timeout'])


class NodeGlobalsConfigTests(unittest.TestCase):
    def test_node_globals_defaults_to_empty_dict(self):
        temp = Path(tempfile.mkdtemp()) / 'config.json'
        store = configure_config(temp)
        self.assertEqual(store.snapshot()['node_globals'], {})

    def test_step_node_mode_survives_patch_and_reload(self):
        temp = Path(tempfile.mkdtemp()) / 'config.json'
        store = configure_config(temp)
        workflows = store.get('workflows')
        workflows['custom_wf'] = {
            'id': 'custom_wf', 'name': '全局节点工作流', 'built_in': False,
            'tray_menu': True,
            'steps': [{'id': 'enable_ipv4', 'enabled': True, 'timeout': 8,
                       'node_mode': 'global'}],
        }
        store.patch({'workflows': workflows})
        reloaded = store.snapshot()['workflows']['custom_wf']['steps'][0]
        self.assertEqual(reloaded['node_mode'], 'global')


if __name__ == '__main__':
    unittest.main()
