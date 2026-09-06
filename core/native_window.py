"""无边框主窗口的原生窗口行为（动画 / 系统贴靠 / 系统命令）。

pywebview frameless 窗口默认没有 WS_CAPTION / WS_THICKFRAME / 最小最大化
盒样式，DWM 因此不播放最小化/最大化动画，也不支持系统贴靠（Win+方向键）。
直接补样式位会让系统绘制非客户区（窗口顶部出现白边）。

标准解法（Chromium / Electron 同款）：
  1. 补齐样式位 WS_THICKFRAME | WS_CAPTION | WS_MINIMIZEBOX | WS_MAXIMIZEBOX；
  2. 子类化窗口过程，对 WM_NCCALCSIZE（wParam=TRUE）返回 0，吞掉整个
     非客户区——系统仍视其为"真窗口"，动画/贴靠/系统命令全部可用，
     视觉上没有任何非客户区；
  3. 最大化时系统会把窗口矩形外扩一个边框宽度，需按边框内缩，
     否则最大化后内容被裁掉一圈。
"""
import logging
import os
import threading
import ctypes
from ctypes import wintypes

logger = logging.getLogger('wifi_tray')

if os.name == 'nt':
    _user32 = ctypes.WinDLL('user32')
    # 返回值/参数按 64 位安全类型声明（句柄/指针不截断）
    _user32.FindWindowW.restype = ctypes.c_void_p
    _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    _user32.CallWindowProcW.restype = ctypes.c_ssize_t
    _user32.CallWindowProcW.argtypes = [ctypes.c_ssize_t, ctypes.c_ssize_t,
                                        ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    _user32.IsZoomed.argtypes = [ctypes.c_void_p]
    _user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_uint]
else:
    _user32 = None

GWL_STYLE = -16
GWLP_WNDPROC = -4
WM_NCCALCSIZE = 0x0083
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_POPUP = 0x80000000
SM_CXSIZEFRAME = 32
SM_CXPADDEDBORDER = 92
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x1, 0x2, 0x4, 0x20

_state = {'installed': False, 'old_proc': 0, 'callback': None}
_state_lock = threading.Lock()


def _has_ptr_api():
    return hasattr(_user32, 'GetWindowLongPtrW')


def _get_style(hwnd, index):
    if _has_ptr_api():
        return _user32.GetWindowLongPtrW(hwnd, index)
    return _user32.GetWindowLongW(hwnd, index)


def _set_style(hwnd, index, value):
    if _has_ptr_api():
        return _user32.SetWindowLongPtrW(hwnd, index, value)
    return _user32.SetWindowLongW(hwnd, index, value)


def _native_wndproc(hwnd, msg, wparam, lparam):
    """子类化窗口过程：只拦截 WM_NCCALCSIZE，其余全部回链原过程。"""
    try:
        if msg == WM_NCCALCSIZE and wparam:
            if _user32.IsZoomed(hwnd):
                # 最大化时系统窗口矩形外扩一个边框宽度，按边框内缩，
                # 让客户区完整落在屏幕内（Chromium 同款处理）
                frame = (_user32.GetSystemMetrics(SM_CXSIZEFRAME)
                         + _user32.GetSystemMetrics(SM_CXPADDEDBORDER))
                # lparam 指向 NCCALCSIZE_PARAMS，首个成员即 rgrc[0]
                rect = wintypes.RECT.from_address(lparam)
                rect.left += frame
                rect.top += frame
                rect.right -= frame
                rect.bottom -= frame
            return 0
    except Exception:
        logger.exception('native wndproc error')
    return _user32.CallWindowProcW(_state['old_proc'], hwnd, msg, wparam, lparam)


def enable_native_window_behaviors(title: str) -> bool:
    """为指定标题的顶层窗口启用原生窗口行为，成功返回 True（已安装则幂等）。"""
    if os.name != 'nt' or _user32 is None:
        return False
    with _state_lock:
        if _state['installed']:
            return True
        try:
            hwnd = _user32.FindWindowW(None, title)
            if not hwnd:
                return False
            style = _get_style(hwnd, GWL_STYLE)
            # 去掉 WS_POPUP（与 WS_CAPTION 组合非法），补齐"真窗口"样式
            style = (style & ~WS_POPUP) | WS_THICKFRAME | WS_CAPTION \
                | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
            _set_style(hwnd, GWL_STYLE, style)

            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_ssize_t,
                                         ctypes.c_uint, ctypes.c_size_t,
                                         ctypes.c_ssize_t)
            _state['callback'] = WNDPROC(_native_wndproc)
            _state['old_proc'] = _get_style(hwnd, GWLP_WNDPROC)
            _set_style(hwnd, GWLP_WNDPROC, ctypes.cast(_state['callback'],
                                                             ctypes.c_void_p).value or 0)

            SWP = SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED
            _user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP)
            _state['installed'] = True
            logger.info('[window] native behaviors enabled (animations/snap/system commands)')
            return True
        except Exception as exc:
            logger.warning('enable_native_window_behaviors failed: %s', exc)
            return False


def enable_native_window_behaviors_with_retry(title: str, attempts: int = 5,
                                              delay: float = 1.0) -> bool:
    """后台线程重试安装（窗口标题注册可能晚于 shown 事件触发）。"""
    def _run():
        for _ in range(attempts):
            if enable_native_window_behaviors(title):
                return
            threading.Event().wait(delay)
    threading.Thread(target=_run, name='native-window', daemon=True).start()
    return _state['installed']
