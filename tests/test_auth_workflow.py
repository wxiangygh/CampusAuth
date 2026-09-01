import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.auth_workflow import (
    ACTIONS, RUNNER, WORKFLOW_CATALOG, resolve_workflow, run_auth_workflow,
    validate_auth_workflow,
)
from core.config import DEFAULT_AUTH_WORKFLOW, _builtin_workflows
from core.workflow import StepResult, StepSpec, WorkflowContext, WorkflowResult


class AuthWorkflowCatalogTests(unittest.TestCase):
    def test_required_fine_grained_nodes_exist(self):
        for node_id in ('portal_logout', 'restart_warp_service', 'start_warp_service',
                        'stop_warp_service', 'set_warp_masque', 'reset_warp_masque',
                        'configure_ipv6_dns', 'wait_public_ipv6'):
            self.assertIn(node_id, ACTIONS)
            self.assertIn(node_id, WORKFLOW_CATALOG)

    def test_restore_network_workflow_is_editable_builtin(self):
        # 恢复网络工作流必须作为内置工作流存在且可编辑（2026-09-01）
        builtin = _builtin_workflows()
        self.assertIn('restore_network', builtin)
        self.assertTrue(builtin['restore_network']['built_in'])
        step_ids = [step['id'] for step in builtin['restore_network']['steps']]
        for expected in ('disconnect_warp', 'stop_warp_service', 'warp_underlay_unpin',
                         'enable_ipv4', 'reset_ipv6_dns', 'refresh_status'):
            self.assertIn(expected, step_ids)
        self.assertIn('warp_underlay_unpin', ACTIONS)
        self.assertIn('warp_underlay_unpin', WORKFLOW_CATALOG)

    def test_builtin_workflows_validate(self):
        for workflow in _builtin_workflows().values():
            steps = validate_auth_workflow(workflow['steps'])
            self.assertTrue(steps)
            self.assertTrue(any(step.enabled for step in steps))

    def test_resolve_unknown_workflow_falls_back_to_default(self):
        config = {'active_workflow_id': 'missing', 'workflows': _builtin_workflows()}
        workflow = resolve_workflow(config, 'missing')
        self.assertEqual(workflow['id'], 'default_auth')

    def test_portal_logout_is_a_workflow_node(self):
        calls = []

        def fake_logout(config, timeout):
            calls.append((config, timeout))
            return True

        context = WorkflowContext(config={'username': 'u'}, cancelled=lambda: False,
                                  overall_timeout=10)
        context.current_attempt = 1
        with patch('core.auth_workflow._auth_helpers',
                   return_value=(None, None, None, fake_logout)):
            result = ACTIONS['portal_logout'](context, StepSpec(id='portal_logout', timeout=6))
        self.assertTrue(result.success)
        self.assertEqual(len(calls), 1)

    def test_restart_warp_service_is_independent_node(self):
        commands = []

        def fake_run_command(command, timeout=None, shell=True):
            commands.append(str(command))
            if 'sc config' in str(command):
                return 0, '', ''
            if 'sc query' in str(command):
                return 0, 'STOPPED', ''
            if 'net start' in str(command):
                return 0, '', ''
            return 0, '', ''

        context = WorkflowContext(config={}, cancelled=lambda: False, overall_timeout=10)
        with patch('core.auth_workflow.run_command', side_effect=fake_run_command):
            result = ACTIONS['restart_warp_service'](
                context, StepSpec(id='restart_warp_service', timeout=10))
        self.assertTrue(result.success)
        self.assertTrue(any('net start "CloudflareWARP"' in command for command in commands))
        self.assertFalse(any('net stop "CloudflareWARP"' in command for command in commands))

    def test_default_granular_workflow_runs_end_to_end_with_mocked_system(self):
        def fake_run_command(command, timeout=None, shell=True):
            text = ' '.join(command) if isinstance(command, (list, tuple)) else str(command)
            if 'show interfaces' in text:
                return 0, 'SSID : Campus\n状态: 已连接', ''
            if 'sc query' in text:
                return 0, 'STOPPED', ''
            return 0, '', ''

        def fake_helpers():
            return (lambda *args, **kwargs: True,
                    lambda *args, **kwargs: True,
                    lambda *args, **kwargs: (True, 'Portal 已认证'),
                    lambda *args, **kwargs: True)

        connect_result = SimpleNamespace(success=True, message='WARP 已连接', code='connected',
                                         attempts=1, elapsed=0.1, retryable=False,
                                         status_output='Status: Connected')
        patches = [
            patch('core.auth_workflow.run_command', side_effect=fake_run_command),
            patch('core.auth_workflow._auth_helpers', side_effect=fake_helpers),
            patch('core.auth_workflow.get_wifi_interface_name', return_value='Wi-Fi'),
            patch('core.auth_workflow.is_warp_connected', return_value=False),
            patch('core.auth_workflow.disconnect_warp', return_value=True),
            patch('core.auth_workflow.has_public_ipv6', return_value=(True, '2001:db8::1')),
            patch('core.auth_workflow._set_warp_endpoint_ipv6', return_value=True),
            patch('core.auth_workflow._set_warp_masque_mode', return_value=True),
            patch('core.auth_workflow.get_warp_cli', return_value='warp-cli.exe'),
            patch('core.auth_workflow.connect_warp_result', return_value=connect_result),
        ]
        for active_patch in patches:
            active_patch.start()
        try:
            context = WorkflowContext(
                config={'wifi_name': 'Campus', 'username': 'u', 'password': 'p'},
                cancelled=lambda: False, overall_timeout=30)
            result = RUNNER.run(DEFAULT_AUTH_WORKFLOW, context, success_message='完成')
        finally:
            for active_patch in patches:
                active_patch.stop()
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.completed_steps[-1], 'refresh_status')
        self.assertIn('enable_ipv4', result.completed_steps)
        self.assertEqual(result.completed_steps.count('enable_ipv4'), 2)


class DetectWarpStateTests(unittest.TestCase):
    """严格模式下 detect_warp_state 不得因"WARP 已连接"而跳过准备步骤。

    2026-09-01 的故障：连接步骤失败后回滚恢复了 IPv4，WARP 随后在后台经
    IPv4 连上；此后每次运行都在本步判定"流程已完成"而跳过全部准备步骤，
    导致"IPv4 开启 + WARP 走 IPv4"的残留状态被一直沿用。
    """

    def _run(self, warp_connected, strict):
        context = WorkflowContext(config={}, cancelled=lambda: False, overall_timeout=10)
        if strict:
            context.data['strict_full_run'] = True
        with patch('core.auth_workflow.is_warp_connected', return_value=warp_connected):
            result = ACTIONS['detect_warp_state'](context,
                                                  StepSpec(id='detect_warp_state', timeout=6))
        return context, result

    def test_strict_mode_runs_full_flow_even_when_connected(self):
        context, result = self._run(warp_connected=True, strict=True)
        self.assertTrue(result.success)
        self.assertFalse(context.data['already_connected'])

    def test_strict_mode_when_disconnected(self):
        context, result = self._run(warp_connected=False, strict=True)
        self.assertTrue(result.success)
        self.assertFalse(context.data['already_connected'])

    def test_non_strict_still_skips_when_connected(self):
        # 未开启严格模式时保留原有优化行为
        context, result = self._run(warp_connected=True, strict=False)
        self.assertTrue(result.success)
        self.assertTrue(context.data['already_connected'])

    def test_non_strict_runs_full_flow_when_disconnected(self):
        context, result = self._run(warp_connected=False, strict=False)
        self.assertTrue(result.success)
        self.assertFalse(context.data['already_connected'])


class StrictFullRunWorkflowTests(unittest.TestCase):
    """端到端：WARP 已连接时，严格模式必须真正执行各准备步骤。"""

    def _run_workflow(self, strict):
        calls = []

        def fake_run_command(command, timeout=None, shell=True):
            text = ' '.join(command) if isinstance(command, (list, tuple)) else str(command)
            if 'show interfaces' in text:
                return 0, 'SSID : Campus\n状态: 已连接', ''
            if 'sc query' in text:
                return 0, 'STOPPED', ''
            return 0, '', ''

        def fake_helpers():
            return (lambda *a, **k: calls.append('disable_ipv4') or True,
                    lambda *a, **k: calls.append('enable_ipv4') or True,
                    lambda *a, **k: (calls.append('portal_login'), (True, 'Portal 已认证'))[1],
                    lambda *a, **k: True)

        connect_result = SimpleNamespace(success=True, message='WARP 已连接', code='connected',
                                         attempts=1, elapsed=0.1, retryable=False,
                                         status_output='Status: Connected')
        patches = [
            patch('core.auth_workflow.run_command', side_effect=fake_run_command),
            patch('core.auth_workflow._auth_helpers', side_effect=fake_helpers),
            patch('core.auth_workflow.get_wifi_interface_name', return_value='Wi-Fi'),
            # WARP 已连接：非严格模式下这会触发"跳过准备步骤"
            patch('core.auth_workflow.is_warp_connected', return_value=True),
            patch('core.auth_workflow.disconnect_warp', return_value=True),
            patch('core.auth_workflow.has_public_ipv6', return_value=(True, '2001:db8::1')),
            patch('core.auth_workflow._set_warp_endpoint_ipv6', return_value=True),
            patch('core.auth_workflow._set_warp_masque_mode', return_value=True),
            patch('core.auth_workflow.get_warp_cli', return_value='warp-cli.exe'),
            patch('core.auth_workflow.connect_warp_result', return_value=connect_result),
        ]
        for active_patch in patches:
            active_patch.start()
        try:
            context = WorkflowContext(
                config={'wifi_name': 'Campus', 'username': 'u', 'password': 'p'},
                cancelled=lambda: False, overall_timeout=30)
            context.data['strict_full_run'] = strict
            result = RUNNER.run(DEFAULT_AUTH_WORKFLOW, context, success_message='完成')
        finally:
            for active_patch in patches:
                active_patch.stop()
        return result, calls

    def test_strict_mode_executes_preparation_steps(self):
        result, calls = self._run_workflow(strict=True)
        self.assertTrue(result.success, result.message)
        self.assertIn('portal_login', calls, f'严格模式应执行 Portal 认证，实际: {calls}')
        self.assertIn('disable_ipv4', calls, f'严格模式应执行禁用 IPv4，实际: {calls}')

    def test_non_strict_mode_skips_preparation_steps(self):
        _, calls = self._run_workflow(strict=False)
        self.assertNotIn('portal_login', calls, f'非严格模式应跳过 Portal 认证，实际: {calls}')
        self.assertNotIn('disable_ipv4', calls, f'非严格模式应跳过禁用 IPv4，实际: {calls}')


class Ipv4BindingDetectionTests(unittest.TestCase):
    """is_ipv4_enabled 供开机/WiFi 事件守卫判断 IPv4 是否处于流程终态。"""

    def test_detects_enabled_binding(self):
        from core.auth import is_ipv4_enabled
        with patch('core.auth.run_command', return_value=(0, 'True\r\n', '')):
            self.assertTrue(is_ipv4_enabled('WLAN'))

    def test_detects_disabled_binding(self):
        from core.auth import is_ipv4_enabled
        with patch('core.auth.run_command', return_value=(0, 'False', '')):
            self.assertFalse(is_ipv4_enabled('WLAN'))

    def test_detection_failure_defaults_to_enabled(self):
        # 探测失败时按"已启用"处理：宁可重走完整流程，也不沿用残留状态
        from core.auth import is_ipv4_enabled
        with patch('core.auth.run_command', return_value=(1, '', '拒绝访问')):
            self.assertTrue(is_ipv4_enabled('WLAN'))


class RunAuthWorkflowStrictTests(unittest.TestCase):
    """run_auth_workflow 必须把严格模式标记写入 context。

    手动点击「开始认证」、开机与 WiFi 事件守卫都经由本函数进入工作流；
    标记没写进去，工作流内部就仍会按"WARP 已连接"跳过准备步骤。
    """

    def _run(self, **kwargs):
        captured = {}
        config = {'workflows': _builtin_workflows(), 'active_workflow_id': 'default_auth'}

        def fake_run(workflow, context, success_message=None):
            captured['strict'] = context.data.get('strict_full_run')
            return WorkflowResult(True, '完成', 'ok', None, 0.0, ())

        patches = [
            patch('core.auth_workflow.RUNNER.run', side_effect=fake_run),
            patch('core.auth_workflow._record_step_stats', lambda *a, **k: []),
            patch('core.auth_workflow.app_state.update_operation', lambda **k: None),
            patch('core.status.network_status.request_refresh', lambda: None),
        ]
        for active_patch in patches:
            active_patch.start()
        try:
            success, message = run_auth_workflow(config=config, **kwargs)
        finally:
            for active_patch in reversed(patches):
                active_patch.stop()
        return captured, success

    def test_strict_enabled_by_default(self):
        captured, success = self._run()
        self.assertTrue(success)
        self.assertTrue(captured['strict'])

    def test_strict_can_be_disabled(self):
        captured, success = self._run(strict=False)
        self.assertTrue(success)
        self.assertFalse(captured['strict'])


class Ipv4FinalStateGuardTests(unittest.TestCase):
    """ipv4_matches_final_state：IPv4 开关需与 auto_enable_ipv4 决定的终态一致。"""

    def _matches(self, interface, ipv4_enabled, auto_enable_ipv4):
        from core.startup import ipv4_matches_final_state
        with patch('core.startup.get_wifi_interface_name', return_value=interface), \
             patch('core.auth.is_ipv4_enabled', return_value=ipv4_enabled):
            return ipv4_matches_final_state({'auto_enable_ipv4': auto_enable_ipv4})

    def test_ipv4_disabled_matches_when_config_keeps_ipv4_off(self):
        self.assertTrue(self._matches('WLAN', ipv4_enabled=False, auto_enable_ipv4=False))

    def test_ipv4_enabled_is_residual_when_config_keeps_ipv4_off(self):
        # 上次连接失败回滚后 IPv4 被恢复 → 残留状态，需要重跑认证
        self.assertFalse(self._matches('WLAN', ipv4_enabled=True, auto_enable_ipv4=False))

    def test_ipv4_enabled_matches_when_config_restores_ipv4(self):
        # 默认配置流程结束会重新启用 IPv4，此时"已启用"才是正常终态
        self.assertTrue(self._matches('WLAN', ipv4_enabled=True, auto_enable_ipv4=True))

    def test_unknown_interface_treated_as_matching(self):
        # 无法判断接口时按一致处理，避免无谓重认证
        self.assertTrue(self._matches('', ipv4_enabled=True, auto_enable_ipv4=False))


class ConnectWarpActionTests(unittest.TestCase):
    def test_failure_aborts_inflight_connection(self):
        # 连接失败后必须中止仍在后台建立的连接，否则回滚恢复 IPv4 后
        # WARP 会经 IPv4 连上，留下"IPv4 开启 + WARP 走 IPv4"的脏状态
        commands = []

        def fake_run_command(command, timeout=None, shell=True):
            commands.append(' '.join(command) if isinstance(command, (list, tuple)) else str(command))
            return 0, '', ''

        fail_result = SimpleNamespace(success=False, code='timeout', message='未连接',
                                      retryable=True, attempts=1, elapsed=1.0,
                                      status_output='')
        context = WorkflowContext(config={}, cancelled=lambda: False, overall_timeout=10)
        with patch('core.auth_workflow.connect_warp_result', return_value=fail_result), \
             patch('core.auth_workflow.get_warp_cli', return_value='warp-cli.exe'), \
             patch('core.auth_workflow.run_command', side_effect=fake_run_command):
            result = ACTIONS['connect_warp'](context, StepSpec(id='connect_warp', timeout=15))
        self.assertFalse(result.success)
        self.assertTrue(any('disconnect' in command for command in commands),
                        f'expected a disconnect command, got: {commands}')


if __name__ == '__main__':
    unittest.main()
