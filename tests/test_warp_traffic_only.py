"""connect_warp 节点连接模式链测试。

官方文档（Client modes / Known limitations）：客户端只有在本机 DNS 代理成功
解析专用主机 connectivity-check.warp-svc 并通过连通性检查后才显示「已连接」。
- warp（流量和DNS）：DNS 代理上游走隧道内，检查可过 → 校园网首选；
- tunnel_only（仅流量）：检查仍依赖本地 DNS 代理的隧道外 DoH 直连
  （校园网封锁 162.159.36.x:443）→ 永远无法通过，卡 Connecting。
默认 auto：先 warp（一次给够预算），失败再退 tunnel_only 兜底（预算 10s），
且本网络确认兜底不可用后 6 小时内直接跳过。
"""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import auth_workflow
from core.auth_workflow import ACTIONS
from core.workflow import StepSpec, WorkflowContext


def _connect_result(success=True):
    return SimpleNamespace(success=success, message='已连接' if success else '失败',
                           code='ok' if success else 'failed', retryable=not success,
                           attempts=1, elapsed=1.0, status_output='')


class ConnectWarpModeChainTests(unittest.TestCase):
    def setUp(self):
        auth_workflow._TUNNEL_ONLY_FAILED_AT.clear()

    def _context(self, config=None, deadline=None):
        # 默认关闭 IPv6 pin，避免测试触发真实 netsh；pin 行为单独测试
        context = WorkflowContext(config={'warp_underlay_ipv6': False, **(config or {})},
                                  cancelled=lambda: False)
        if deadline is not None:
            context.deadline = time.monotonic() + deadline
        return context

    def test_auto_tries_warp_mode_first(self):
        """默认 auto：先切「流量和DNS」（warp）模式并连接成功。"""
        commands = []
        context = self._context()
        with patch('core.auth_workflow.get_warp_cli', return_value='warp-cli.exe'), \
             patch('core.auth_workflow.run_command',
                   side_effect=lambda cmd, **kw: (commands.append(cmd), (0, '', ''))[1]), \
             patch('core.auth_workflow.connect_warp_result',
                   return_value=_connect_result(True)) as connect:
            result = ACTIONS['connect_warp'](context,
                                             StepSpec(id='connect_warp', timeout=15))
        self.assertTrue(result.success, result.message)
        self.assertEqual(commands, [['warp-cli.exe', 'mode', 'warp']])
        connect.assert_called_once()
        self.assertIn('流量和DNS模式', result.message)
        self.assertTrue(context.data['warp_connected'])

    def test_auto_falls_back_to_tunnel_only(self):
        """auto：warp 模式失败后断开残留连接，退到 tunnel_only 再试。"""
        commands = []
        context = self._context(deadline=60)
        with patch('core.auth_workflow.get_warp_cli', return_value='warp-cli.exe'), \
             patch('core.auth_workflow._network_fingerprint', return_value='net'), \
             patch('core.auth_workflow.run_command',
                   side_effect=lambda cmd, **kw: (commands.append(cmd), (0, '', ''))[1]), \
             patch('core.auth_workflow.connect_warp_result',
                   side_effect=[_connect_result(False), _connect_result(True)]) as connect:
            result = ACTIONS['connect_warp'](context,
                                             StepSpec(id='connect_warp', timeout=60))
        self.assertTrue(result.success, result.message)
        self.assertEqual(commands, [['warp-cli.exe', 'mode', 'warp'],
                                    ['warp-cli.exe', 'disconnect'],
                                    ['warp-cli.exe', 'mode', 'tunnel_only']])
        self.assertEqual(connect.call_count, 2)
        self.assertIn('仅流量模式', result.message)
        # 首选模式拿走大部分预算（8 成减去兜底预留），兜底只留 10s
        first_timeout = connect.call_args_list[0].kwargs['timeout']
        second_timeout = connect.call_args_list[1].kwargs['timeout']
        self.assertGreaterEqual(first_timeout, 44.0)
        self.assertLessEqual(first_timeout, 49.0)
        self.assertEqual(second_timeout, 10.0)

    def test_all_modes_fail_reports_last_result(self):
        """auto 两个模式都失败：逐个断开并以最后一次结果报告失败。"""
        commands = []
        context = self._context(deadline=60)
        with patch('core.auth_workflow.get_warp_cli', return_value='warp-cli.exe'), \
             patch('core.auth_workflow._network_fingerprint', return_value='net'), \
             patch('core.auth_workflow.run_command',
                   side_effect=lambda cmd, **kw: (commands.append(cmd), (0, '', ''))[1]), \
             patch('core.auth_workflow.connect_warp_result',
                   return_value=_connect_result(False)) as connect:
            result = ACTIONS['connect_warp'](context,
                                             StepSpec(id='connect_warp', timeout=60))
        self.assertFalse(result.success)
        self.assertEqual(connect.call_count, 2)
        # 每次模式失败后都断开，避免半开隧道干扰下一模式/回滚
        self.assertEqual(commands.count(['warp-cli.exe', 'disconnect']), 2)
        self.assertEqual(commands[-1], ['warp-cli.exe', 'disconnect'])
        # 兜底失败（预算 ≥8s）应记入本网络记忆，供后续跳过
        self.assertIn('net', auth_workflow._TUNNEL_ONLY_FAILED_AT)

    def test_tunnel_only_skipped_when_remembered(self):
        """本网络 6 小时内已确认兜底失败 → 跳过 tunnel_only，不切模式不重连。"""
        auth_workflow._TUNNEL_ONLY_FAILED_AT['net'] = time.monotonic()
        commands = []
        context = self._context(deadline=60)
        with patch('core.auth_workflow.get_warp_cli', return_value='warp-cli.exe'), \
             patch('core.auth_workflow._network_fingerprint', return_value='net'), \
             patch('core.auth_workflow.run_command',
                   side_effect=lambda cmd, **kw: (commands.append(cmd), (0, '', ''))[1]), \
             patch('core.auth_workflow.connect_warp_result',
                   return_value=_connect_result(False)) as connect:
            result = ACTIONS['connect_warp'](context,
                                             StepSpec(id='connect_warp', timeout=60))
        self.assertFalse(result.success)
        connect.assert_called_once()  # 只有 warp 一次尝试
        self.assertEqual(commands, [['warp-cli.exe', 'mode', 'warp'],
                                    ['warp-cli.exe', 'disconnect']])

    def test_stale_memory_does_not_skip_fallback(self):
        """记忆超过 6 小时失效 → 兜底照常尝试。"""
        auth_workflow._TUNNEL_ONLY_FAILED_AT['net'] = time.monotonic() - 7 * 3600
        context = self._context(deadline=60)
        with patch('core.auth_workflow.get_warp_cli', return_value='warp-cli.exe'), \
             patch('core.auth_workflow._network_fingerprint', return_value='net'), \
             patch('core.auth_workflow.run_command', return_value=(0, '', '')), \
             patch('core.auth_workflow.connect_warp_result',
                   return_value=_connect_result(False)) as connect:
            ACTIONS['connect_warp'](context, StepSpec(id='connect_warp', timeout=60))
        self.assertEqual(connect.call_count, 2)

    def test_floored_failure_not_remembered(self):
        """总时限耗尽后的塌缩预算（5s）失败不作数，不写入记忆。"""
        context = self._context()  # 无 deadline：remaining 回落为 1.0 → 预算 5s
        with patch('core.auth_workflow.get_warp_cli', return_value='warp-cli.exe'), \
             patch('core.auth_workflow._network_fingerprint', return_value='net'), \
             patch('core.auth_workflow.run_command', return_value=(0, '', '')), \
             patch('core.auth_workflow.connect_warp_result',
                   return_value=_connect_result(False)) as connect:
            ACTIONS['connect_warp'](context, StepSpec(id='connect_warp', timeout=60))
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(auth_workflow._TUNNEL_ONLY_FAILED_AT, {})

    def test_explicit_step_param_overrides_mode(self):
        """节点参数 warp_mode='tunnel_only' 时不再尝试 warp 模式。"""
        commands = []
        context = self._context()
        with patch('core.auth_workflow.get_warp_cli', return_value='warp-cli.exe'), \
             patch('core.auth_workflow.run_command',
                   side_effect=lambda cmd, **kw: (commands.append(cmd), (0, '', ''))[1]), \
             patch('core.auth_workflow.connect_warp_result',
                   return_value=_connect_result(True)) as connect:
            result = ACTIONS['connect_warp'](
                context, StepSpec(id='connect_warp', timeout=15,
                                  params={'warp_mode': 'tunnel_only'}))
        self.assertTrue(result.success, result.message)
        self.assertEqual(commands, [['warp-cli.exe', 'mode', 'tunnel_only']])
        connect.assert_called_once()

    def test_mode_switch_failure_does_not_block_connect(self):
        """模式切换失败（老版本 warp-cli）不阻断连接。"""
        context = self._context()
        with patch('core.auth_workflow.get_warp_cli', return_value='warp-cli.exe'), \
             patch('core.auth_workflow.run_command',
                   return_value=(-1, '', 'unrecognized subcommand')), \
             patch('core.auth_workflow.connect_warp_result',
                   return_value=_connect_result(True)) as connect:
            result = ACTIONS['connect_warp'](context,
                                             StepSpec(id='connect_warp', timeout=15))
        self.assertTrue(result.success, result.message)
        connect.assert_called_once()

    def test_already_connected_does_not_change_mode(self):
        """WARP 已连接（skip 路径）时不扰动当前模式。"""
        commands = []
        context = self._context()
        context.data['already_connected'] = True
        with patch('core.auth_workflow.get_warp_cli', return_value='warp-cli.exe'), \
             patch('core.auth_workflow.run_command',
                   side_effect=lambda cmd, **kw: (commands.append(cmd), (0, '', ''))[1]), \
             patch('core.auth_workflow.connect_warp_result',
                   return_value=_connect_result(True)) as connect:
            result = ACTIONS['connect_warp'](context,
                                             StepSpec(id='connect_warp', timeout=15))
        self.assertTrue(result.success, result.message)
        self.assertEqual(commands, [])  # 未下发任何模式切换命令
        connect.assert_not_called()
        self.assertTrue(context.data['warp_connected'])

    def test_missing_warp_cli_attempts_connect_once(self):
        """warp-cli 缺失时不切模式，只尝试一次连接（连接自身给出失败原因）。"""
        context = self._context()
        with patch('core.auth_workflow.get_warp_cli', return_value=None), \
             patch('core.auth_workflow.connect_warp_result',
                   return_value=_connect_result(False)) as connect:
            result = ACTIONS['connect_warp'](context,
                                             StepSpec(id='connect_warp', timeout=15))
        connect.assert_called_once()
        self.assertFalse(result.success)

    def test_underlay_pin_created_before_connect(self):
        """IPv6 pin 在连接尝试之前建立（顺序：pin → 模式切换 → 连接）。"""
        calls = []
        context = self._context(config={'warp_underlay_ipv6': True})
        with patch('core.auth_workflow.get_warp_cli', return_value='warp-cli.exe'), \
             patch('core.auth_workflow.run_command',
                   side_effect=lambda cmd, **kw:
                       (calls.append('cmd:' + ' '.join(cmd)), (0, '', ''))[1]), \
             patch('warp_exclusion.ensure_warp_underlay_ipv6_pin',
                   side_effect=lambda: (calls.append('pin'), (True, 'pinned'))[1]), \
             patch('core.auth_workflow.connect_warp_result',
                   side_effect=lambda **kw:
                       (_connect_result(True), calls.append('connect'))[0]):
            result = ACTIONS['connect_warp'](context,
                                             StepSpec(id='connect_warp', timeout=15))
        self.assertTrue(result.success, result.message)
        self.assertEqual(calls, ['pin', 'cmd:warp-cli.exe mode warp', 'connect'])


if __name__ == '__main__':
    unittest.main()
