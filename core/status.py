"""Single-source network status probing and background synchronization."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from core.app_state import app_state
from core.command import run_command
from core.network import _check_internet, get_wifi_interface_name
from core.warp_manager import probe_warp_status

logger = logging.getLogger("wifi_tray")


def probe_network_status() -> dict:
    """Probe WARP and IPv4 once, using bounded command timeouts."""
    warp = probe_warp_status(timeout=3)
    warp_connected = warp.success
    interface_name = get_wifi_interface_name() or "WLAN"
    command = f'(Get-NetAdapterBinding -Name "{interface_name}" -ComponentID ms_tcpip).Enabled'
    code, output, error = run_command(
        ['powershell', '-NoProfile', '-Command', command], shell=False, timeout=3)
    ipv4_disabled = code == 0 and 'False' in output
    if not warp_connected and ipv4_disabled:
        adapter_command = ('Get-NetAdapter -Name *WARP* | Where-Object { $_.Status -eq "Up" } '
                           '| Select-Object -First 1 -ExpandProperty Name')
        adapter_code, adapter_output, _ = run_command(
            ['powershell', '-NoProfile', '-Command', adapter_command], shell=False, timeout=3)
        warp_connected = adapter_code == 0 and bool(adapter_output.strip())

    details = {
        'warp_connected': warp_connected,
        'warp_code': warp.code,
        'ipv4_disabled': ipv4_disabled,
        'interface': interface_name,
        'probe_error': error.strip()[:160] if error else '',
    }
    if warp_connected and ipv4_disabled:
        return {'status': 'connected', 'message': 'WARP已连接，IPv4已禁用', **details}
    if warp_connected:
        return {'status': 'partial', 'message': 'WARP已连接，IPv4同时可用', **details}
    if ipv4_disabled:
        message = 'IPv4已禁用但WARP未连接'
        if warp.code == 'registration_required':
            message = warp.message
        return {'status': 'broken', 'message': message, **details}
    if _check_internet(timeout=1.5):
        return {'status': 'normal', 'message': '正常模式', **details}
    return {'status': 'disconnected', 'message': '未检测到可用网络', **details}


class NetworkStatusService:
    """Coalesces status checks and publishes them to one observable hub."""

    def __init__(self, probe: Callable[[], dict] = probe_network_status,
                 idle_interval: float = 6.0, busy_interval: float = 2.0):
        self.probe = probe
        self.idle_interval = idle_interval
        self.busy_interval = busy_interval
        self._refresh_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

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
            return app_state.snapshot()['network']
        finally:
            self._refresh_lock.release()

    def request_refresh(self) -> None:
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
            interval = self.busy_interval if operation.get('status') == 'running' else self.idle_interval
            self._wake.wait(interval)
            self._wake.clear()


network_status = NetworkStatusService()
