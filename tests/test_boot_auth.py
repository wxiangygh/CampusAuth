"""开机自动认证测试：无线先连配置的 WiFi、有线直接认证、仅开机自启执行。"""
import unittest
from unittest.mock import patch

from core import startup
from core.network import connect_wifi


def _cfg(**over):
    base = {'auto_auth': True, 'wifi_name': 'CampusNet', 'auto_enable_ipv4': True}
    base.update(over)
    return base


class ShouldRunBootAuthTests(unittest.TestCase):
    """静默启动只针对开机：手动启动（非 --silent）绝不自动认证。"""

    def test_runs_only_when_silent_and_enabled(self):
        self.assertTrue(startup.should_run_boot_auth(_cfg(), silent=True))
        self.assertFalse(startup.should_run_boot_auth(_cfg(), silent=False))
        self.assertFalse(
            startup.should_run_boot_auth(_cfg(auto_auth=False), silent=True))

    def test_disabled_by_default(self):
        with patch.object(startup, 'get_config', return_value=_cfg(auto_auth=False)):
            self.assertFalse(startup.should_run_boot_auth(None, silent=True))


class ConnectWifiTests(unittest.TestCase):
    def test_empty_ssid_rejected(self):
        ok, msg = connect_wifi('   ')
        self.assertFalse(ok)
        self.assertIn('未配置', msg)

    def test_no_wireless_adapter(self):
        with patch('core.network.get_wifi_interface_name', return_value=None):
            ok, msg = connect_wifi('CampusNet')
        self.assertFalse(ok)
        self.assertIn('无线网卡', msg)

    def test_connect_command_failure_returns_netsh_error(self):
        with patch('core.network.get_wifi_interface_name', return_value='WLAN'),              patch('core.network.run_command',
                   return_value=(1, '', '没有配置文件 "Nope"')):
            ok, msg = connect_wifi('Nope')
        self.assertFalse(ok)
        self.assertIn('Nope', msg)

    def test_success_when_ssid_matches(self):
        def fake_run(cmd, **kw):
            if 'wlan connect' in cmd:
                return 0, '', ''
            return 0, '    SSID                  : CampusNet', ''
        with patch('core.network.get_wifi_interface_name', return_value='WLAN'),              patch('core.network.run_command', side_effect=fake_run),              patch('core.network.time.sleep'):
            ok, msg = connect_wifi('CampusNet', timeout=10)
        self.assertTrue(ok, msg)
        self.assertEqual(msg, '已连接')

    def test_settled_on_other_network_fails_early(self):
        def fake_run(cmd, **kw):
            if 'wlan connect' in cmd:
                return 0, '', ''
            return 0, '    SSID                  : OtherNet', ''
        with patch('core.network.get_wifi_interface_name', return_value='WLAN'),              patch('core.network.run_command', side_effect=fake_run),              patch('core.network.time.sleep'),              patch('core.network.time.time',
                   side_effect=[0, 1, 2, 3, 9, 10, 11, 12]):
            ok, msg = connect_wifi('CampusNet', timeout=25)
        self.assertFalse(ok)
        self.assertIn('OtherNet', msg)

    def test_timeout_when_never_connects(self):
        def fake_run(cmd, **kw):
            if 'wlan connect' in cmd:
                return 0, '', ''
            return 0, '', ''  # 一直无 SSID
        with patch('core.network.get_wifi_interface_name', return_value='WLAN'),              patch('core.network.run_command', side_effect=fake_run),              patch('core.network.time.sleep'),              patch('core.network.time.time',
                   side_effect=[0] + [i * 2 + 1 for i in range(40)]):
            ok, msg = connect_wifi('CampusNet', timeout=25)
        self.assertFalse(ok)
        self.assertIn('超时', msg)


class BootAuthFlowTests(unittest.TestCase):
    """check_startup_wifi_and_auth：按链路类型分流。"""

    def _flow(self, cfg, link, current_ssid, connect_result=(True, '已连接'),
              warp_connected=False):
        """跑一遍开机认证，返回 (connect_mock, auth_mock) 便于断言。"""
        with patch.object(startup, 'get_config', return_value=cfg),              patch.object(startup, 'is_warp_connected',
                       return_value=warp_connected),              patch.object(startup, 'ipv4_matches_final_state',
                       return_value=True),              patch.object(startup, 'resolve_active_interface',
                       return_value=link),              patch.object(startup, 'get_current_wifi_ssid',
                       return_value=current_ssid),              patch.object(startup, 'connect_wifi',
                       return_value=connect_result) as connect,              patch('core.auth.run_auth_task',
                   return_value=(True, '认证成功')) as auth,              patch.object(startup, '_auth_lock') as lock,              patch.object(startup, 'update_tray_icon'):
            lock.acquire.return_value = True
            startup.check_startup_wifi_and_auth()
        return connect, auth

    def test_wired_skips_wifi_and_auths_directly(self):
        connect, auth = self._flow(_cfg(wifi_name=''), ('wired', '以太网'), '')
        connect.assert_not_called()
        auth.assert_called_once()

    def test_wireless_on_target_auths_without_reconnecting(self):
        connect, auth = self._flow(_cfg(), ('wireless', 'WLAN'), 'CampusNet')
        connect.assert_not_called()
        auth.assert_called_once()

    def test_wireless_connects_configured_wifi_then_auths(self):
        connect, auth = self._flow(_cfg(), ('wireless', 'WLAN'), '')
        connect.assert_called_once_with('CampusNet')
        auth.assert_called_once()

    def test_wireless_connect_failure_aborts_auth(self):
        connect, auth = self._flow(_cfg(), ('wireless', 'WLAN'), '',
                                   connect_result=(False, '连接超时'))
        connect.assert_called_once()
        auth.assert_not_called()

    def test_wireless_without_configured_wifi_skips(self):
        connect, auth = self._flow(_cfg(wifi_name=''), ('wireless', 'WLAN'), '')
        connect.assert_not_called()
        auth.assert_not_called()

    def test_warp_final_state_skips_everything(self):
        connect, auth = self._flow(_cfg(), ('wireless', 'WLAN'), '',
                                   warp_connected=True)
        connect.assert_not_called()
        auth.assert_not_called()


class ElevatedLaunchTests(unittest.TestCase):
    """提权重启必须以 SW_SHOWNORMAL 显示新进程：nShowCmd=0(SW_HIDE) 会写进
    STARTUPINFO，覆盖新进程第一次 ShowWindow，导致提权后主窗体"闪现即隐藏"。"""

    def test_shell_execute_uses_sw_shownormal(self):
        from core import startup
        self.assertEqual(startup.SW_SHOWNORMAL, 1)
        source = (startup.__file__ and
                  open(startup.__file__, encoding='utf-8').read())
        self.assertIn('ShellExecuteW', source)
        self.assertNotIn(', None, 0\n        )', source)


if __name__ == '__main__':
    unittest.main()
