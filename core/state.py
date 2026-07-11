"""全局状态变量集中管理。

所有模块通过 from core.state import X 来访问共享状态，
避免全局变量散落在各文件中导致状态不一致。
"""
import threading

# WiFi 事件名称（用于跨进程事件通知）
WIFI_EVENT_NAME = "Global\\WiFiAutoAuth_WiFiEvent"

# 认证流程控制
_auth_lock = threading.Lock()
_auth_cancelled = threading.Event()

# WiFi 事件监视
_wifi_event_handle = None
_wifi_monitor_started = False

# WARP 配置备份（_set_warp_endpoint_ipv6 使用）
_conf_json_backup = None

# 托盘应用实例引用（由 tray_app.py 的 main() 设置）
_tray_app_instance = None

# 单例控制互斥锁句柄（由 check_single_instance 设置，on_exit 释放）
TRAY_MUTEX = None
