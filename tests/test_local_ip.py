"""IPv4 地址解析测试：有线物理网卡地址必须优先于 WARP 隧道/socket 回退。

背景（2026-09-06）：WARP 全隧道在线时，get_local_ip 的 socket 回退落进隧道，
取到 WARP 虚拟网卡的 172.16.x 被过滤 → 返回空，状态条 IPv4 显示「—」，
而物理网卡（以太网 2）明明持有校园网地址 172.21.77.91。
"""
import unittest
from unittest.mock import patch

from core import network as net


IPCONFIG_WIRED = '''无线局域网适配器 WLAN:

   媒体状态  . . . . . . . . . . . . : 媒体已断开连接

以太网适配器 以太网 2:

   连接特定的 DNS 后缀 . . . . . . . : proxy.bjut.edu.cn
   IPv6 地址 . . . . . . . . . . . . : 2001:da8:216:214d:a47b:728:3f27:1dc2
   本地链接 IPv6 地址 . . . . . . . : fe80::cad3:9d31:fcde:641e%7
   IPv4 地址 . . . . . . . . . . . . : 172.21.77.91
   子网掩码  . . . . . . . . . . . . : 255.255.255.0
   默认网关. . . . . . . . . . . . . : 172.21.77.254

以太网适配器 CloudflareWARP:

   连接特定的 DNS 后缀 . . . . . . . :
   IPv4 地址 . . . . . . . . . . . . : 172.16.0.2
'''


def _fake_run(output):
    return lambda cmd, **kw: (0, output, '')


class GetAdapterIpv4Tests(unittest.TestCase):
    def test_wired_adapter_returns_campus_ipv4(self):
        with patch.object(net, 'run_command', side_effect=_fake_run(IPCONFIG_WIRED)):
            self.assertEqual(net.get_adapter_ipv4('以太网 2'), '172.21.77.91')

    def test_adapter_name_suffix_not_confused(self):
        """「以太网」不得命中「以太网适配器 以太网 2:」的段。"""
        with patch.object(net, 'run_command', side_effect=_fake_run(IPCONFIG_WIRED)):
            self.assertEqual(net.get_adapter_ipv4('以太网'), '')

    def test_warp_virtual_ip_filtered(self):
        """WARP 虚拟网卡的 172.16.x 必须过滤，不能当成物理网卡地址。"""
        with patch.object(net, 'run_command', side_effect=_fake_run(IPCONFIG_WIRED)):
            self.assertEqual(net.get_adapter_ipv4('CloudflareWARP'), '')

    def test_disconnected_adapter_returns_empty(self):
        with patch.object(net, 'run_command', side_effect=_fake_run(IPCONFIG_WIRED)):
            self.assertEqual(net.get_adapter_ipv4('WLAN'), '')

    def test_empty_interface_name(self):
        self.assertEqual(net.get_adapter_ipv4(''), '')


class GetLocalIpTests(unittest.TestCase):
    def test_prefers_active_wired_adapter(self):
        """活动网卡是有线时直接解析其 ipconfig 段，不依赖 socket 回退。"""
        with patch.object(net, 'resolve_active_interface',
                          return_value=('wired', '以太网 2')), \
             patch.object(net, 'run_command', side_effect=_fake_run(IPCONFIG_WIRED)):
            self.assertEqual(net.get_local_ip(), '172.21.77.91')

    def test_falls_back_to_socket_when_adapter_has_no_ipv4(self):
        """活动网卡没有 IPv4（如已禁用绑定）时退回 socket 路径。"""
        output = '以太网适配器 以太网 2:\n\n   媒体状态 . . . : 媒体已断开连接\n'
        with patch.object(net, 'resolve_active_interface',
                          return_value=('wired', '以太网 2')), \
             patch.object(net, 'run_command', side_effect=_fake_run(output)), \
             patch.object(net, 'get_wifi_interface_name', return_value=''), \
             patch('socket.socket') as sock:
            sock.return_value.getsockname.return_value = ('10.0.0.5', 0)
            self.assertEqual(net.get_local_ip(), '10.0.0.5')


if __name__ == '__main__':
    unittest.main()


class CheckInternetTests(unittest.TestCase):
    """多目标连通性探测：单一目标不可达不误报离线（8.8.8.8 被校园网封锁的常见场景）。"""

    def test_fallback_to_second_target(self):
        calls = []

        def fake_connect(addr, timeout=0):
            calls.append(addr[0])
            if addr[0] == '119.29.29.29':
                return object()
            raise OSError('unreachable')

        with patch('socket.create_connection', side_effect=fake_connect):
            self.assertTrue(net._check_internet(timeout=0.1))
        self.assertEqual(calls[0], '223.5.5.5')  # 首选国内目标
        self.assertIn('119.29.29.29', calls)

    def test_all_targets_fail_reports_offline(self):
        with patch('socket.create_connection', side_effect=OSError('down')):
            self.assertFalse(net._check_internet(timeout=0.05))

    def test_ipv6_targets_included(self):
        hosts = []

        def fake_connect(addr, timeout=0):
            hosts.append(addr[0])
            raise OSError('down')

        with patch('socket.create_connection', side_effect=fake_connect):
            net._check_internet(timeout=0.05)
        self.assertTrue(any(':' in h for h in hosts))  # 含 IPv6 字面量目标


class HasIpv6GatewayTests(unittest.TestCase):
    """has_ipv6_gateway：RA 宣告（IPv6 默认网关）检测。

    背景（2026-09-07）：宿舍 AP 上游 IPv6 断供时，WLAN 上有 fe80 默认网关
    （RA 宣告）但没有任何全局地址；认证页仍显示 9月5日 的历史租约。
    有网关 + 无公网地址 = 网络侧前缀/DHCPv6 未下发，与完全没发 IPv6
    （连网关都没有）要区分开。
    """

    WLAN_RA_GATEWAY = '''无线局域网适配器 WLAN:

   连接特定的 DNS 后缀 . . . . . . . : 
   本地链接 IPv6 地址. . . . . . . . : fe80::4d91:b8b2:ed03:195f%28
   IPv4 地址 . . . . . . . . . . . . : 10.121.25.113
   子网掩码  . . . . . . . . . . . . : 255.255.240.0
   默认网关. . . . . . . . . . . . . : fe80::a4f:aff:fec8:6c79%28
                                       10.121.31.254
'''

    def test_fe80_gateway_counts_as_advertisement(self):
        # 真实断供形态（2026-09-07 宿舍 AP）：fe80 网关在标签行。
        # Windows 只在收到带路由器生存期的 RA 时才安装 fe80 默认路由，
        # 因此 fe80 网关本身就是「路由器在宣告」的证据
        with patch.object(net, 'run_command', return_value=(0, self.WLAN_RA_GATEWAY, '')):
            found, gateway = net.has_ipv6_gateway()
        self.assertTrue(found)
        self.assertEqual(gateway, 'fe80::a4f:aff:fec8:6c79')

    def test_global_v6_gateway_counts(self):
        output = self.WLAN_RA_GATEWAY.replace('fe80::a4f:aff:fec8:6c79%28',
                                              '2001:da8:216::1')
        with patch.object(net, 'run_command', return_value=(0, output, '')):
            found, gateway = net.has_ipv6_gateway()
        self.assertTrue(found)
        self.assertEqual(gateway, '2001:da8:216::1')

    def test_ipv4_only_gateway_not_counted(self):
        output = self.WLAN_RA_GATEWAY.replace(
            '默认网关. . . . . . . . . . . . . : fe80::a4f:aff:fec8:6c79%28',
            '默认网关. . . . . . . . . . . . . : 10.121.31.254')
        with patch.object(net, 'run_command', return_value=(0, output, '')):
            self.assertEqual(net.has_ipv6_gateway(), (False, ''))

    def test_empty_gateway_not_counted(self):
        output = self.WLAN_RA_GATEWAY.replace(
            '默认网关. . . . . . . . . . . . . : fe80::a4f:aff:fec8:6c79%28\n'
            '                                       10.121.31.254',
            '默认网关. . . . . . . . . . . . . :')
        with patch.object(net, 'run_command', return_value=(0, output, '')):
            self.assertEqual(net.has_ipv6_gateway(), (False, ''))

    def test_english_default_gateway_counts(self):
        output = '   Default Gateway . . . . . . . . . : 2606:4700::1\n'
        with patch.object(net, 'run_command', return_value=(0, output, '')):
            found, gateway = net.has_ipv6_gateway()
        self.assertTrue(found)
        self.assertEqual(gateway, '2606:4700::1')

    def test_ipconfig_failure_is_no_gateway(self):
        with patch.object(net, 'run_command', return_value=(1, '', 'err')):
            self.assertEqual(net.has_ipv6_gateway(), (False, ''))
