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
