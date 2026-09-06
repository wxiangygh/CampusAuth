"""免流状态判定与免流失效监测测试。

免流语义：WARP 隧道底层走校园网免费 IPv6 才算免流成功。
- IPv4 禁用 或 底层 pin 防火墙规则存在 → 底层必走 IPv6 → warp_free；
- 两者皆无 → 底层走 IPv4（计费）→ 未免流，状态跌出时必须及时提醒。
"""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import status as status_module
from core.status import NetworkStatusService, invalidate_probe_cache


def _warp(success=True, code='ok'):
    return SimpleNamespace(success=success, code=code, message='已连接' if success else '未连接')


def _result(status='connected', message='', **extra):
    base = {'status': status, 'message': message, 'warp_free': False,
            'warp_connected': False, 'ipv4_disabled': False}
    base.update(extra)
    return base


class ProbeFreeTests(unittest.TestCase):
    """probe_network_status 的免流判定分支。"""

    def setUp(self):
        invalidate_probe_cache()

    def _probe(self, *, ipv4_disabled, pinned, warp_connected=True):
        with patch.object(status_module, 'resolve_active_interface',
                          return_value=('wired', '以太网')), \
             patch.object(status_module, 'is_ipv4_enabled',
                          return_value=not ipv4_disabled), \
             patch.object(status_module, 'probe_warp_status', return_value=_warp(warp_connected)), \
             patch('warp_exclusion.is_warp_underlay_pinned', return_value=pinned):
            return status_module.probe_network_status()

    def test_ipv4_disabled_means_free(self):
        """IPv4 已禁用：底层只能走 IPv6 → 免流（且不需要查 pin）。"""
        with patch.object(status_module, '_underlay_pinned',
                          side_effect=AssertionError('不应查询 pin')) as pin:
            result = self._probe(ipv4_disabled=True, pinned=False)
        pin.assert_not_called()
        self.assertEqual(result['status'], 'connected')
        self.assertTrue(result['warp_free'])
        self.assertEqual(result['warp_underlay'], 'ipv6')

    def test_pin_active_means_free_even_with_ipv4(self):
        """IPv4 可用但 pin 规则在：warp-svc 被封禁 IPv4 端点 → 免流。"""
        result = self._probe(ipv4_disabled=False, pinned=True)
        self.assertEqual(result['status'], 'connected')
        self.assertTrue(result['warp_free'])
        self.assertEqual(result['warp_underlay'], 'ipv6')

    def test_no_pin_with_ipv4_is_not_free(self):
        """IPv4 可用且无 pin：底层走 IPv4 计费 → 未免流（partial 告警）。"""
        result = self._probe(ipv4_disabled=False, pinned=False)
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['warp_free'])
        self.assertEqual(result['warp_underlay'], 'ipv4')
        self.assertIn('未免流', result['message'])

    def test_disconnected_without_warp_is_normal(self):
        with patch.object(status_module, '_check_internet', return_value=True):
            result = self._probe(ipv4_disabled=False, pinned=False, warp_connected=False)
        self.assertEqual(result['status'], 'normal')
        self.assertFalse(result['warp_free'])


class UiVisibilityTests(unittest.TestCase):
    """托盘低功耗模式：共享 UI 可见标志驱动各服务降频（隐藏不降功能）。"""

    def setUp(self):
        from core import state as core_state
        core_state.set_ui_visible(True)  # 隔离：每个用例从可见态出发

    def tearDown(self):
        from core import state as core_state
        core_state.set_ui_visible(True)

    def test_state_flag_roundtrip(self):
        from core import state as core_state
        core_state.set_ui_visible(False)
        self.assertFalse(core_state.is_ui_visible())
        core_state.set_ui_visible(True)
        self.assertTrue(core_state.is_ui_visible())

    def test_service_interval_visible(self):
        service = status_module.NetworkStatusService()
        self.assertEqual(service._current_interval(False), service.idle_interval)
        self.assertEqual(service._current_interval(True), service.busy_interval)

    def test_service_interval_hidden_slows_polling(self):
        from core import state as core_state
        service = status_module.NetworkStatusService()
        core_state.set_ui_visible(False)
        self.assertGreaterEqual(service._current_interval(False), 20.0)
        self.assertGreaterEqual(service._current_interval(True), 5.0)

    def test_service_set_ui_visible_propagates_to_shared_flag(self):
        service = status_module.NetworkStatusService()
        service.set_ui_visible(False)
        from core import state as core_state
        self.assertFalse(core_state.is_ui_visible())


class ProbeCacheTests(unittest.TestCase):
    """昂贵子探测的 TTL 缓存：短周期轮询不重复跑 PowerShell。"""

    def setUp(self):
        invalidate_probe_cache()

    def test_ipv4_check_cached_within_ttl(self):
        calls = []

        def counting(interface_name, timeout=8):
            calls.append(interface_name)
            return False

        with patch.object(status_module, 'resolve_active_interface',
                          return_value=('wired', '以太网')), \
             patch.object(status_module, 'is_ipv4_enabled', side_effect=counting), \
             patch.object(status_module, 'probe_warp_status', return_value=_warp(True)):
            status_module.probe_network_status()
            status_module.probe_network_status()
        self.assertEqual(len(calls), 1)  # 第二次命中缓存
        invalidate_probe_cache()
        with patch.object(status_module, 'resolve_active_interface',
                          return_value=('wired', '以太网')), \
             patch.object(status_module, 'is_ipv4_enabled', side_effect=counting), \
             patch.object(status_module, 'probe_warp_status', return_value=_warp(True)):
            status_module.probe_network_status()
        self.assertEqual(len(calls), 2)  # invalidate 后重新探测


class FreeLossMonitorTests(unittest.TestCase):
    """免流失效监测：跌出免流状态触发回调（迟滞 + 抑制）。"""

    def setUp(self):
        self.notifications = []
        self.service = NetworkStatusService(probe=lambda: _result(),
                                            idle_interval=1, busy_interval=1)
        self.service.on_free_dropped = self.notifications.append

    def _feed(self, result):
        with patch.object(self.service, 'probe', return_value=result):
            self.service.refresh()

    def _free(self):
        return _result('connected', '免流中', warp_free=True, warp_connected=True,
                       ipv4_disabled=True)

    def _lost(self, **extra):
        return _result('disconnected', '未连接', warp_free=False,
                       warp_connected=False, ipv4_disabled=False, **extra)

    def test_notify_once_after_two_consecutive_losses(self):
        self._feed(self._free())
        self._feed(self._lost())          # 第 1 次未免流：迟滞中
        self.assertEqual(self.notifications, [])
        self._feed(self._lost())          # 第 2 次：确认失效
        self.assertEqual(len(self.notifications), 1)
        self.assertIn('免流', self.notifications[0])

    def test_no_duplicate_within_remind_interval(self):
        self._feed(self._free())
        for _ in range(4):
            self._feed(self._lost())
        self.assertEqual(len(self.notifications), 1)  # 持续失效只提醒一次

    def test_rearm_after_recovery(self):
        self._feed(self._free())
        self._feed(self._lost())
        self._feed(self._lost())
        self.assertEqual(len(self.notifications), 1)
        self._feed(self._free())          # 恢复免流
        self._feed(self._lost())          # 再次跌出：重新确认
        self._feed(self._lost())
        self.assertEqual(len(self.notifications), 2)

    def test_suppressed_during_running_operation(self):
        from core.app_state import app_state
        self._feed(self._free())
        op_id = app_state.start_operation('auth')
        try:
            self._feed(self._lost())
            self._feed(self._lost())
            self.assertEqual(self.notifications, [])
        finally:
            app_state.update_operation(status='idle', operation_id=op_id)

    def test_suppressed_after_manual_disconnect(self):
        self._feed(self._free())
        with patch('core.state.warp_manual_disconnect_at', return_value=time.time()):
            self._feed(self._lost())
            self._feed(self._lost())
        self.assertEqual(self.notifications, [])

    def test_never_free_never_notifies(self):
        for _ in range(3):
            self._feed(self._lost())
        self.assertEqual(self.notifications, [])

    def test_ipv4_underlay_loss_reason(self):
        """WARP 还在但底层切到 IPv4：原因文案必须点明计费。"""
        self._feed(self._free())
        lost = _result('partial', '未免流', warp_free=False, warp_connected=True,
                       ipv4_disabled=False)
        self._feed(lost)
        self._feed(lost)
        self.assertEqual(len(self.notifications), 1)
        self.assertIn('IPv4', self.notifications[0])
        self.assertIn('计费', self.notifications[0])


if __name__ == '__main__':
    unittest.main()
