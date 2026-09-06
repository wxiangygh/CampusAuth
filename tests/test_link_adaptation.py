"""节点链路（有线/无线）动态适配回归测试。

覆盖用户关注的「重置 IPv6 DNS 为 DHCP 等节点是否动态适配有线/无线」：
- resolve_active_interface 的链路判定；
- 工作流节点经 _interface() 按 link_type 选网卡；
- 状态探测与内置恢复逻辑在有线链路下的行为。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.auth_workflow import ACTIONS, _interface
from core.network import resolve_active_interface
from core.workflow import StepSpec, WorkflowContext


WLAN_CONNECTED_OUTPUT = (
    '名称: WLAN\n'
    '描述: Intel(R) Wi-Fi 6 AX201\n'
    '状态                  : 已连接\n'
    'SSID                  : CampusNet-5G\n'
)

WLAN_DISCONNECTED_OUTPUT = (
    '接口名称: WLAN\n'
    '状态                  : 已断开连接\n'
)


class ResolveActiveInterfaceTests(unittest.TestCase):
    def test_wlan_connected_returns_wireless_with_name(self):
        with patch('core.network.run_command',
                   return_value=(0, WLAN_CONNECTED_OUTPUT, '')):
            link, name = resolve_active_interface()
        self.assertEqual(link, 'wireless')
        self.assertEqual(name, 'WLAN')

    def test_wlan_down_wired_up_returns_wired(self):
        def fake_run_command(cmd, **kwargs):
            text = cmd if isinstance(cmd, str) else ' '.join(cmd)
            if 'wlan' in text:
                return 0, WLAN_DISCONNECTED_OUTPUT, ''
            # Get-NetAdapter 输出：Name|Status（get_wired_interface_name 解析格式）
            return 0, '以太网|Up\n', ''
        with patch('core.network.run_command', side_effect=fake_run_command):
            link, name = resolve_active_interface()
        self.assertEqual(link, 'wired')
        self.assertEqual(name, '以太网')

    def test_wifi_direct_connected_without_ssid_is_ignored(self):
        """Wi-Fi Direct 虚拟接口"已连接"但无 SSID：不算无线联网
        （纯有线机器曾因此被误判为 wireless，导致恢复流程找 WLAN）。"""
        output = (
            '接口名称: WLAN\n'
            '状态                  : 已断开连接\n'
            '接口名称: 本地连接* 1\n'
            '状态                  : 已连接\n'
            'SSID                  : \n'
        )
        with patch('core.network.run_command', return_value=(0, output, '')):
            link, name = resolve_active_interface()
        self.assertEqual(link, 'wired')

    def test_connected_without_ssid_falls_back_to_wired_in_interface(self):
        """节点网卡选择：判定 wireless 但系统无可用 WLAN 时按有线自愈。"""
        from core.auth_workflow import ACTIONS, _interface
        context = WorkflowContext(config={}, cancelled=lambda: False)
        context.data['link_type'] = 'wireless'
        step = StepSpec(id='enable_ipv4', params={})
        with patch('core.auth_workflow.get_wifi_interface_name', return_value=None), \
             patch('core.auth_workflow.get_wired_interface_name',
                   return_value='以太网 2') as wired:
            name, error = _interface(context, step)
        self.assertIsNone(error)
        self.assertEqual(name, '以太网 2')
        wired.assert_called_once()

    def test_no_link_falls_back_to_wireless(self):
        def fake_run_command(cmd, **kwargs):
            text = cmd if isinstance(cmd, str) else ' '.join(cmd)
            if 'wlan' in text:
                return 0, WLAN_DISCONNECTED_OUTPUT, ''
            return 0, '', ''
        with patch('core.network.run_command', side_effect=fake_run_command), \
             patch('core.network.get_wifi_interface_name', return_value='WLAN'):
            link, name = resolve_active_interface()
        self.assertEqual(link, 'wireless')
        self.assertEqual(name, 'WLAN')


class _InterfaceLinkSelectionTests(unittest.TestCase):
    def test_wired_link_uses_wired_adapter(self):
        context = WorkflowContext(config={}, cancelled=lambda: False)
        context.data['link_type'] = 'wired'
        with patch('core.auth_workflow.get_wired_interface_name',
                   return_value='以太网') as wired:
            name, error = _interface(context)
        self.assertIsNone(error)
        self.assertEqual(name, '以太网')
        wired.assert_called_once()

    def test_wireless_link_uses_wifi_adapter(self):
        context = WorkflowContext(config={}, cancelled=lambda: False)
        context.data['link_type'] = 'wireless'
        with patch('core.auth_workflow.get_wifi_interface_name',
                   return_value='WLAN') as wifi:
            name, error = _interface(context)
        self.assertIsNone(error)
        self.assertEqual(name, 'WLAN')
        wifi.assert_called_once()

    def test_wired_missing_adapter_fails_with_retryable(self):
        context = WorkflowContext(config={}, cancelled=lambda: False)
        context.data['link_type'] = 'wired'
        with patch('core.auth_workflow.get_wired_interface_name', return_value=None):
            name, error = _interface(context)
        self.assertIsNone(name)
        self.assertEqual(error.code, 'interface_missing')
        self.assertTrue(error.retryable)


class ResetIpv6DnsLinkTests(unittest.TestCase):
    """「重置 IPv6 DNS 为 DHCP」节点必须按链路类型选网卡。"""

    def test_reset_ipv6_dns_targets_wired_adapter(self):
        context = WorkflowContext(config={}, cancelled=lambda: False)
        context.data['link_type'] = 'wired'
        with patch('core.auth_workflow.get_wired_interface_name', return_value='以太网'), \
             patch('core.auth_workflow.run_command',
                   return_value=(0, '', '')) as run_mock:
            result = ACTIONS['reset_ipv6_dns'](context, StepSpec(id='reset_ipv6_dns'))
        self.assertTrue(result.success, result.message)
        command = run_mock.call_args[0][0]
        self.assertIn('以太网', command)
        self.assertIn('dhcp', command)

    def test_reset_ipv6_dns_targets_wifi_adapter(self):
        context = WorkflowContext(config={}, cancelled=lambda: False)
        context.data['link_type'] = 'wireless'
        with patch('core.auth_workflow.get_wifi_interface_name', return_value='WLAN'), \
             patch('core.auth_workflow.run_command',
                   return_value=(0, '', '')) as run_mock:
            result = ACTIONS['reset_ipv6_dns'](context, StepSpec(id='reset_ipv6_dns'))
        self.assertTrue(result.success, result.message)
        command = run_mock.call_args[0][0]
        self.assertIn('WLAN', command)
        self.assertIn('dhcp', command)

    def test_configure_ipv6_dns_targets_wired_adapter(self):
        """相邻的「设置 IPv6 DNS」节点同样需要有线适配（同一 _interface 路径）。"""
        context = WorkflowContext(config={}, cancelled=lambda: False)
        context.data['link_type'] = 'wired'
        with patch('core.auth_workflow.get_wired_interface_name', return_value='以太网'), \
             patch('core.auth_workflow.run_command',
                   return_value=(0, '', '')) as run_mock:
            result = ACTIONS['configure_ipv6_dns'](context, StepSpec(id='configure_ipv6_dns'))
        self.assertTrue(result.success, result.message)
        all_commands = ' '.join(str(call.args[0]) for call in run_mock.call_args_list)
        self.assertIn('以太网', all_commands)
        self.assertIn('2606:4700:4700::1111', all_commands)
        self.assertIn('2606:4700:4700::1001', all_commands)


class ProbeNetworkStatusLinkTests(unittest.TestCase):
    """状态探测在有线链路下应检查有线网卡的 IPv4 绑定。"""

    def test_probe_checks_wired_interface_when_wired(self):
        from core import status as status_module

        status_module.invalidate_probe_cache()  # 清探测缓存，隔离其他用例
        recorded = {}

        def fake_is_ipv4_enabled(interface_name, timeout=8):
            recorded['interface'] = interface_name
            return False

        warp_status = SimpleNamespace(success=True, code='ok', message='connected')
        with patch.object(status_module, 'resolve_active_interface',
                          return_value=('wired', '以太网')), \
             patch.object(status_module, 'is_ipv4_enabled',
                          side_effect=fake_is_ipv4_enabled), \
             patch.object(status_module, 'probe_warp_status',
                          return_value=warp_status):
            result = status_module.probe_network_status()
        self.assertEqual(recorded['interface'], '以太网')
        self.assertEqual(result['status'], 'connected')
        self.assertEqual(result['link_type'], 'wired')


class RunRestoreTaskLinkTests(unittest.TestCase):
    """内置恢复逻辑（未绑定工作流时）必须按链路类型选网卡。"""

    def test_restore_targets_wired_adapter(self):
        import core.auth as auth_module

        enabled = []

        def fake_enable_ipv4(interface_name, timeout=15):
            enabled.append(interface_name)
            return True

        with patch.object(auth_module, 'resolve_active_interface',
                          return_value=('wired', '以太网')), \
             patch.object(auth_module, 'enable_ipv4', side_effect=fake_enable_ipv4), \
             patch.object(auth_module, 'disconnect_warp', return_value=True), \
             patch.object(auth_module, 'run_command',
                          return_value=(0, 'IPv4 地址 192.168.1.5', '')), \
             patch.object(auth_module, '_interruptible_sleep', return_value=True), \
             patch('warp_exclusion.remove_warp_underlay_ipv6_pin',
                   return_value=(True, '')), \
             patch('urllib.request.urlopen'):
            success, message = auth_module.run_restore_task()
        self.assertTrue(success, message)
        self.assertEqual(enabled, ['以太网'])

    def test_restore_resets_ipv6_dns_on_same_adapter(self):
        """IPv6 DNS 重置（DHCP）必须与启用的网卡一致（有线场景）。"""
        import core.auth as auth_module

        commands = []

        def fake_run_command(cmd, **kwargs):
            commands.append(cmd if isinstance(cmd, str) else ' '.join(cmd))
            return 0, 'IPv4 地址 192.168.1.5', ''

        with patch.object(auth_module, 'resolve_active_interface',
                          return_value=('wired', '以太网')), \
             patch.object(auth_module, 'enable_ipv4', return_value=True), \
             patch.object(auth_module, 'disconnect_warp', return_value=True), \
             patch.object(auth_module, 'run_command', side_effect=fake_run_command), \
             patch.object(auth_module, '_interruptible_sleep', return_value=True), \
             patch('warp_exclusion.remove_warp_underlay_ipv6_pin',
                   return_value=(True, '')), \
             patch('urllib.request.urlopen'):
            success, message = auth_module.run_restore_task()
        self.assertTrue(success, message)
        dns_reset = [c for c in commands if 'ipv6' in c and 'dhcp' in c]
        self.assertTrue(dns_reset, '内置恢复应重置 IPv6 DNS 为 DHCP')
        self.assertIn('以太网', dns_reset[0])


if __name__ == '__main__':
    unittest.main()
