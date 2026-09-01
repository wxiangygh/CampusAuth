"""WARP 断线自动重连看门狗。

周期检测 WARP 连接状态：当 WARP 处于"意外断开"状态持续超过阈值时自动重连。
"主动断开"（用户点恢复/断开 WARP）由 ``core.state`` 的标记记录，看门狗据此跳过，
避免刚点完"恢复网络"又被看门狗立刻拉回 WARP。

设计要点：
- ``evaluate_reconnect`` 是纯函数，负责"要不要重连"的判定，便于单测；
- 看门狗线程复用 ``core.state._auth_lock``，与手动认证/恢复互斥，防止并发冲突；
- 重连失败后重新计时，避免在不可用的网络上风暴式重试。
"""
from __future__ import annotations

import logging
import threading
import time

import core.state
from core.app_state import app_state
from core.config import get_config
from core.warp_manager import connect_warp_result, probe_warp_status

logger = logging.getLogger('wifi_tray')

# 这些状态无法通过自动重连解决（主动断开 / 未注册 / DoH 被封锁），
# 命中时既不触发重连，也不累计断线时长。
NO_RECONNECT_CODES = frozenset({
    'manual_disconnection',
    'registration_required',
    'dns_lookup_failed',
})


def evaluate_reconnect(status_code: str, disconnected_since: float | None,
                       now: float, reconnect_delay: float,
                       manual_disconnect_at: float = 0.0) -> tuple[bool, float | None]:
    """判断当前是否应触发自动重连。

    Args:
        status_code: ``probe_warp_status`` 返回的 code。
        disconnected_since: 上次进入"意外断开"的时间戳（monotonic），None 表示未断开。
        now: 当前 monotonic 时间。
        reconnect_delay: 判定为"意外断开"所需的持续时长（秒）。
        manual_disconnect_at: 最近一次主动断开的时间戳（epoch，0 表示从未主动断开）。

    Returns:
        (should_reconnect, new_disconnected_since)。
    """
    if status_code == 'connected':
        return False, None
    if status_code in NO_RECONNECT_CODES or manual_disconnect_at > 0:
        # 主动断开 / 终态错误：不重连，也不累计断线时长。
        return False, None
    if disconnected_since is None:
        # 首次观察到意外断开，开始计时。
        return False, now
    if now - disconnected_since >= reconnect_delay:
        # 超过阈值，触发重连并清零计时。
        return True, None
    return False, disconnected_since


class ReconnectWatchdog:
    """后台线程：周期探测 WARP 状态，意外断开超阈值时自动重连。"""

    def __init__(self, check_interval: float = 5.0):
        self.check_interval = check_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._disconnected_since: float | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name='warp-reconnect-watchdog', daemon=True)
        self._thread.start()
        logger.info('WARP reconnect watchdog started')

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
        logger.info('WARP reconnect watchdog stopped')

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception('Reconnect watchdog tick failed')
            self._stop.wait(self.check_interval)

    def _tick(self) -> None:
        cfg = get_config()
        if not cfg.get('warp_auto_reconnect'):
            # 开关关闭：看门狗空闲，不计时（切到自动重连时重新开始计时）。
            self._disconnected_since = None
            return

        status = probe_warp_status(timeout=4)
        now = time.monotonic()
        delay = max(1.0, float(cfg.get('warp_reconnect_delay', 20) or 20))
        should, since = evaluate_reconnect(
            status.code, self._disconnected_since, now, delay,
            manual_disconnect_at=core.state.warp_manual_disconnect_at())
        self._disconnected_since = since
        if not should:
            return

        logger.info('WARP 意外断开超过 %.0f 秒（%s），尝试自动重连', delay, status.code)
        if not core.state._auth_lock.acquire(blocking=False):
            logger.info('Auth lock busy, skip auto-reconnect this round')
            self._disconnected_since = time.monotonic()
            return
        try:
            result = connect_warp_result(timeout=30, max_attempts=1)
            if result.success:
                logger.info('WARP 自动重连成功')
                app_state.update_network('connected', 'WARP 已自动重连', warp_connected=True)
                self._refresh_status()
            else:
                logger.warning('WARP 自动重连失败：%s', result.code)
                # 重连失败重新计时，避免风暴式重试。
                self._disconnected_since = time.monotonic()
        finally:
            core.state._auth_lock.release()

    @staticmethod
    def _refresh_status() -> None:
        # 延迟导入，避免与 core.status 形成循环依赖。
        try:
            from core.status import network_status
            network_status.request_refresh()
        except Exception:
            logger.debug('Failed to request network status refresh', exc_info=True)


_watchdog: ReconnectWatchdog | None = None


def start_watchdog() -> None:
    global _watchdog
    if _watchdog is None:
        _watchdog = ReconnectWatchdog()
    _watchdog.start()


def stop_watchdog() -> None:
    global _watchdog
    if _watchdog is not None:
        _watchdog.stop()
