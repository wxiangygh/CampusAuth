"""Single-source network status probing and background synchronization.

免流语义：本应用的主要用途是「免流」——只有 WARP 隧道的底层走校园网免费
IPv6 时才算免流成功。判定依据：
- IPv4 已禁用 → warp-svc 只能走 IPv6 端点 → 免流；
- IPv4 可用但底层 pin 防火墙规则在（封锁 warp-svc 访问 Cloudflare IPv4
  端点段，见 warp_exclusion.py）→ 隧道底层只能走 IPv6 → 免流；
- 两者皆无 → 隧道底层很可能走 IPv4（计费）→ 未免流，需要告警。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from core.app_state import app_state
from core.auth import is_ipv4_enabled
from core.command import run_command
from core.network import _check_internet, resolve_active_interface
from core.warp_manager import probe_warp_status

logger = logging.getLogger("wifi_tray")

# 昂贵子探测的 TTL 缓存：probe 每 2~30s 跑一次，PowerShell 系检查（IPv4 绑定、
# 防火墙规则）冷启动可达数秒，不缓存会把整个刷新周期拖到 10s+，状态严重滞后。
# TTL 取值兼顾及时性与资源：网卡/IPv4 变更只可能由本应用的工作流或用户手动操作
# 触发，工作流结束会调用 invalidate() 立即清缓存，15s/60s 足够新鲜。
_PROBE_CACHE: dict[tuple, tuple[float, object]] = {}
_PROBE_CACHE_LOCK = threading.Lock()
_IPV4_CACHE_TTL = 15.0
_IFACE_CACHE_TTL = 15.0
_PIN_CACHE_TTL = 60.0


def _cached(key: tuple, ttl: float, fn: Callable[[], object]) -> object:
    now = time.monotonic()
    with _PROBE_CACHE_LOCK:
        hit = _PROBE_CACHE.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _PROBE_CACHE_LOCK:
        _PROBE_CACHE[key] = (time.monotonic(), value)
    return value


def invalidate_probe_cache() -> None:
    """清空探测缓存：工作流改动网卡/pin 后立即调用，保证下一次探测拿新值。"""
    with _PROBE_CACHE_LOCK:
        _PROBE_CACHE.clear()


def probe_network_status() -> dict:
    """Probe WARP and IPv4 once, using bounded command timeouts."""
    warp = probe_warp_status(timeout=3)
    warp_connected = warp.success
    # 链路感知选接口：有线联网时 IPv4 绑定检查必须落在有线网卡上，
    # 否则认证流程禁用了有线网卡的 IPv4，这里却还在查 WLAN，永远误报 partial
    link_type, interface_name = _cached(('iface',), _IFACE_CACHE_TTL,
                                        resolve_active_interface)
    # 复用 auth.is_ipv4_enabled（timeout=8、严格解析）。此前这里内联
    # timeout=3 的探测在本机 PowerShell 冷启动约 5s，必然超时，导致
    # IPv4 已禁用却被误报为"同时可用"（partial）。
    ipv4_disabled = not _cached(('ipv4', interface_name), _IPV4_CACHE_TTL,
                                lambda: is_ipv4_enabled(interface_name))
    if not warp_connected and ipv4_disabled:
        adapter_command = ('Get-NetAdapter -Name *WARP* | Where-Object { $_.Status -eq "Up" } '
                           '| Select-Object -First 1 -ExpandProperty Name')
        adapter_code, adapter_output, _ = run_command(
            ['powershell', '-NoProfile', '-Command', adapter_command], shell=False, timeout=3)
        warp_connected = adapter_code == 0 and bool(adapter_output.strip())

    # 免流判定：WARP 连接的底层是否锁定在 IPv6（见模块 docstring）
    underlay = 'unknown'
    if warp_connected:
        if ipv4_disabled:
            underlay = 'ipv6'
        elif _cached(('pin',), _PIN_CACHE_TTL, _underlay_pinned):
            underlay = 'ipv6'
        else:
            underlay = 'ipv4'
    warp_free = warp_connected and underlay == 'ipv6'

    details = {
        'warp_connected': warp_connected,
        'warp_code': warp.code,
        'warp_underlay': underlay,
        'warp_free': warp_free,
        'ipv4_disabled': ipv4_disabled,
        'interface': interface_name,
        'link_type': link_type,
    }
    if warp_connected and warp_free:
        how = 'IPv4 已禁用' if ipv4_disabled else 'IPv4 端点已被防火墙锁定'
        return {'status': 'connected', 'message': f'免流中：WARP 走 IPv6 底层（{how}）', **details}
    if warp_connected:
        return {'status': 'partial',
                'message': 'WARP已连接但底层走 IPv4，未免流（按校园网计费）', **details}
    if ipv4_disabled:
        message = 'IPv4已禁用但WARP未连接'
        if warp.code == 'registration_required':
            message = warp.message
        return {'status': 'broken', 'message': message, **details}
    if _check_internet(timeout=1.5):
        return {'status': 'normal', 'message': '正常模式（未开启免流）', **details}
    return {'status': 'disconnected', 'message': '未检测到可用网络', **details}


def _underlay_pinned() -> bool:
    try:
        from warp_exclusion import is_warp_underlay_pinned
        return bool(is_warp_underlay_pinned())
    except Exception:
        logger.exception('underlay pin check failed')
        return False


class NetworkStatusService:
    """Coalesces status checks and publishes them to one observable hub.

    免流失效监测：warp_free 从 True 跌出（WARP 断开或底层切到 IPv4）时回调
    on_free_dropped(原因)，供托盘气泡与前端 toast 提醒用户及时恢复，避免
    流量悄悄走 IPv4 计费。带迟滞（连续 2 次未免流才确认）与抑制（用户主动
    断开、工作流执行中不提醒；失效持续期间每 10 分钟最多提醒一次）。
    """

    # 失效确认所需连续探测次数（防瞬时抖动刷屏）
    FREE_LOSS_CONFIRM_PROBES = 2
    # 失效持续期间的重复提醒间隔（秒）
    FREE_LOSS_REMIND_INTERVAL = 600.0

    def __init__(self, probe: Callable[[], dict] = probe_network_status,
                 idle_interval: float = 6.0, busy_interval: float = 2.0):
        self.probe = probe
        self.idle_interval = idle_interval
        self.busy_interval = busy_interval
        self.on_free_dropped: Callable[[str], None] | None = None
        # 主窗口可见性统一存 core.state（set_ui_visible），此处不再持副本
        self._refresh_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ever_free = False
        self._free_loss_probes = 0
        self._free_loss_last_notify = 0.0

    def set_ui_visible(self, visible: bool) -> None:
        """主窗口 显示/隐藏到托盘 时由 UI 层调用，切换轮询频率。

        可见性存到 core.state 共享标志（流量监控、看门狗等同样读取），
        本服务额外唤醒轮询线程让模式切换立即生效。
        """
        from core import state as core_state
        core_state.set_ui_visible(visible)
        self._ui_visible = bool(visible)
        self._wake.set()  # 模式切换立即生效

    def _current_interval(self, operation_running: bool) -> float:
        from core import state as core_state
        visible = core_state.is_ui_visible()
        if operation_running:
            return self.busy_interval if visible else max(self.busy_interval, 5.0)
        return self.idle_interval if visible else max(self.idle_interval * 5.0, 20.0)

    def snapshot(self) -> dict:
        return app_state.snapshot()['network']

    def refresh(self) -> dict:
        if not self._refresh_lock.acquire(blocking=False):
            return self.snapshot()
        try:
            app_state.set_network_checking()
            try:
                result = self.probe()
            except Exception as exc:
                logger.exception('Network status probe failed')
                result = {'status': 'unknown', 'message': f'状态检测失败：{exc}'}
            app_state.update_network(**result)
            try:
                self._check_free_loss(result)
            except Exception:
                logger.exception('free-loss check failed')
            return app_state.snapshot()['network']
        finally:
            self._refresh_lock.release()

    def request_refresh(self) -> None:
        self._wake.set()

    def invalidate(self) -> None:
        """探测缓存失效 + 立即刷新：工作流改完网卡/WARP 后调用，状态马上跟上。"""
        invalidate_probe_cache()
        self._wake.set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='network-status', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            operation = app_state.snapshot()['operation']
            interval = self._current_interval(operation.get('status') == 'running')
            self._wake.wait(interval)
            self._wake.clear()

    def _check_free_loss(self, result: dict) -> None:
        free = bool(result.get('warp_free'))
        if free:
            self._ever_free = True
            self._free_loss_probes = 0
            # 恢复免流即重置提醒时钟：再次跌出属于新事件，必须立刻提醒；
            # 频繁抖动由「连续 2 次才确认」的迟滞挡住，不会刷屏
            self._free_loss_last_notify = 0.0
            return
        # 尚未免流过（启动初期/从未成功）：没有「掉」可言
        if not self._ever_free:
            return
        # 用户主动行为期间的波动不提醒：手动断开未清理、或操作（认证/恢复）执行中
        try:
            from core.state import warp_manual_disconnect_at
            if warp_manual_disconnect_at() > 0:
                return
        except Exception:
            pass
        if app_state.snapshot()['operation'].get('status') == 'running':
            return
        self._free_loss_probes += 1
        if self._free_loss_probes < self.FREE_LOSS_CONFIRM_PROBES:
            return
        now = time.monotonic()
        if now - self._free_loss_last_notify < self.FREE_LOSS_REMIND_INTERVAL:
            return
        self._free_loss_last_notify = now
        if result.get('warp_connected'):
            reason = '免流失效：WARP 已改走 IPv4 底层，流量正在按校园网计费'
        elif result.get('ipv4_disabled'):
            reason = '免流已断开：WARP 未连接且 IPv4 已禁用，当前无网络'
        else:
            reason = '免流已断开：WARP 未连接，流量正在走 IPv4 计费'
        logger.warning('[free-loss] %s', reason)
        if self.on_free_dropped is not None:
            try:
                self.on_free_dropped(reason)
            except Exception:
                logger.exception('free-loss notify callback failed')


network_status = NetworkStatusService()
