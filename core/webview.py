"""WebView 窗口创建辅助模块。

集中管理 Win32 窗口置顶逻辑和 WebView 窗口创建，
消除 tray_app.py 中 3 处重复的窗口创建代码。
"""
import ctypes
import logging
import webview

logger = logging.getLogger('wifi_tray')

# Win32 常量集中定义
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
SW_RESTORE = 9


def bring_window_to_top(title):
    """将指定标题的窗口置顶并激活。

    Args:
        title: 窗口标题字符串

    Returns:
        True 如果找到并激活窗口，False 如果未找到窗口
    """
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    user32.SetForegroundWindow(hwnd)
    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE)
    return True


def create_webview_window(api, title, html_file, width, height,
                          resizable=True, frameless=True,
                          on_closing_callback=None):
    """创建 WebView 窗口。

    Args:
        api: ApiBridge 实例，暴露给前端 JS
        title: 窗口标题
        html_file: HTML 文件完整路径
        width: 窗口宽度
        height: 窗口高度
        resizable: 是否可调整大小
        frameless: 是否无边框
        on_closing_callback: 窗口关闭时的回调函数

    Returns:
        webview.Window 实例，或 None 如果创建失败
    """
    try:
        win = webview.create_window(
            title, html_file, js_api=api,
            width=width, height=height,
            resizable=resizable, frameless=frameless,
        )
        if on_closing_callback:
            win.events.closing += on_closing_callback
        return win
    except Exception as e:
        logger.error(f"create_webview_window: 创建窗口失败: {e}")
        return None
