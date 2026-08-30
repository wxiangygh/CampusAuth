import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.auth_workflow import (
    ACTIONS, RUNNER, WORKFLOW_CATALOG, resolve_workflow, validate_auth_workflow,
)
from core.config import DEFAULT_AUTH_WORKFLOW, _builtin_workflows
from core.workflow import StepSpec, WorkflowContext


class AuthWorkflowCatalogTests(unittest.TestCase):
    def test_required_fine_grained_nodes_exist(self):
        for node_id in ('portal_logout', 'restart_warp_service', 'start_warp_service',
                        'stop_warp_service', 'set_warp_masque', 'reset_warp_masque',
                        'configure_ipv6_dns', 'wait_public_ipv6'):
            self.assertIn(node_id, ACTIONS)
            self.assertIn(node_id, WORKFLOW_CATALOG)

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


if __name__ == '__main__':
    unittest.main()
