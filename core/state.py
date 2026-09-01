"""全局状态变量集中管理。

所有模块通过 from core.state import X 来访问共享状态，
避免全局变量散落在各文件中导致状态不一致。
"""
import threading
import time

# WiFi 事件名称（用于跨进程事件通知）
WIFI_EVENT_NAME = "Global\\WiFiAutoAuth_WiFiEvent"

# 认证流程控制
_auth_lock = threading.Lock()
_auth_cancelled = threading.Event()

# WARP 主动断开标记（自动重连看门狗用于区分"主动断开"与"意外断开"）。
# 用户/应用主动调用 disconnect_warp 时记录时间戳；连接成功后清零。
# 看门狗据此跳过主动断开场景，仅在 WARP 意外掉线时自动重连。
_warp_manual_disconnect_at = 0.0
_warp_manual_disconnect_lock = threading.Lock()


def mark_warp_manual_disconnect():
    """记录一次主动断开（用户手动恢复/断开 WARP）。"""
    global _warp_manual_disconnect_at
    with _warp_manual_disconnect_lock:
        _warp_manual_disconnect_at = time.time()


def clear_warp_manual_disconnect():
    """连接成功后清除主动断开标记，恢复看门狗对意外断开的感知。"""
    global _warp_manual_disconnect_at
    with _warp_manual_disconnect_lock:
        _warp_manual_disconnect_at = 0.0


def warp_manual_disconnect_at() -> float:
    """返回最近一次主动断开的时间戳（epoch，0 表示从未主动断开）。"""
    with _warp_manual_disconnect_lock:
        return _warp_manual_disconnect_at

# WiFi 事件监视
_wifi_event_handle = None
_wifi_monitor_started = False
_wifi_monitor_stop = threading.Event()
_wifi_monitor_thread = None

# WARP 配置备份（_set_warp_endpoint_ipv6 使用）
_conf_json_backup = None

# 托盘应用实例引用（由 tray_app.py 的 main() 设置）
_tray_app_instance = None

# 单例控制互斥锁句柄（由 check_single_instance 设置，on_exit 释放）
TRAY_MUTEX = None
