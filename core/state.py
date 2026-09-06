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

# 最近一次工作流运行结果（WorkflowResult，含每节点耗时/重试统计）。
# 仅用于「测试工作流」悬浮窗展示；run_auth_workflow 每次运行后覆盖。
last_workflow_result = None

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

# 主窗口可见性（用户视角）：隐藏到托盘后，所有"看不见就不必跑"的周期性
# 工作（状态探测、流量快照、看门狗）统一据此降频或暂停，省 CPU/功耗。
# 功能不缩水：恢复可见时各处立即刷新一次，数据即时跟上。
_ui_visible = True
_ui_visible_lock = threading.Lock()


def set_ui_visible(visible: bool) -> None:
    """主窗口 显示/隐藏到托盘 时由 UI 层调用（tray_app 各 show/hide 入口）。"""
    global _ui_visible
    with _ui_visible_lock:
        _ui_visible = bool(visible)


def is_ui_visible() -> bool:
    with _ui_visible_lock:
        return _ui_visible

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
