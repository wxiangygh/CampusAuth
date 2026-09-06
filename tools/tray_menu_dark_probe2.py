"""托盘菜单深色主题诊断（插桩版）：定位"回浅失败"的确切环节。

每次显示记录：
1. uxtheme 当前 ShouldAppsUseDarkMode（132）读数；
2. 显示前已存在的菜单窗口句柄、销毁结果、销毁后是否仍存在；
3. 显示中菜单窗口句柄（是否复用旧窗口）；
4. WM_CREATE 钩子是否捕获创建（native_theme 的 INFO 日志）；
5. 对已存在/新窗口手工应用主题时 AllowDarkModeForWindow/SetWindowTheme
   的返回值（浅色用显式 'Explorer' 与 NULL 两种对照）。
"""
import ctypes
import logging
import time

from ctypes import wintypes
from PIL import ImageGrab

from core import native_theme
from core.native_theme import (
    destroy_menu_window, set_menu_dark_mode, theme_popup_menu_window,
    MENU_WINDOW_CLASS,
)

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

user32 = ctypes.WinDLL('user32')
kernel32 = ctypes.WinDLL('kernel32')
user32.CreateWindowExW.restype = ctypes.c_void_p
user32.FindWindowW.restype = ctypes.c_void_p
user32.TrackPopupMenuEx.restype = ctypes.c_bool
user32.TrackPopupMenuEx.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_int, ctypes.c_int,
                                    ctypes.c_void_p, ctypes.c_void_p]
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint,
                            ctypes.c_void_p]
uxtheme = ctypes.WinDLL('uxtheme')
uxtheme.SetWindowTheme.restype = ctypes.c_long  # HRESULT
uxtheme.SetWindowTheme.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                   ctypes.c_wchar_p]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetProcAddress.restype = ctypes.c_void_p
kernel32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

TPM_RIGHTALIGN = 0x0008
TPM_BOTTOMALIGN = 0x0020
TPM_RETURNCMD = 0x0100

TIMERPROC = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint,
                               ctypes.c_size_t, wintypes.DWORD)
_capture = {'rect': None, 'shot': None, 'hwnd': None}


def _uxtheme_proc(ordinal):
    handle = kernel32.GetModuleHandleW('uxtheme')
    if not handle:
        return None
    return kernel32.GetProcAddress(handle, ctypes.c_void_p(ordinal))


def should_apps_use_dark_mode():
    proc = _uxtheme_proc(132)
    if not proc:
        return None
    fn = ctypes.WINFUNCTYPE(ctypes.c_long)
    return bool(ctypes.cast(proc, fn)())


def allow_dark_for_window(hwnd, dark):
    proc = _uxtheme_proc(133)
    if not proc:
        return None
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_long)
    return ctypes.cast(proc, fn)(hwnd, 1 if dark else 0)


def menu_hwnd():
    return user32.FindWindowW(MENU_WINDOW_CLASS, None)


class RECT(ctypes.Structure):
    _fields_ = [('left', wintypes.LONG), ('top', wintypes.LONG),
                ('right', wintypes.LONG), ('bottom', wintypes.LONG)]


def _timer_proc(hwnd, msg, timer_id, tick):
    try:
        hwnd_menu = menu_hwnd()
        _capture['hwnd'] = hwnd_menu
        if hwnd_menu:
            rect = RECT()
            user32.GetWindowRect(hwnd_menu, ctypes.byref(rect))
            _capture['rect'] = (rect.left, rect.top, rect.right, rect.bottom)
        _capture['shot'] = ImageGrab.grab()
        user32.EndMenu()
    except Exception as exc:
        print(f'定时器回调异常: {exc}')
    finally:
        user32.KillTimer(hwnd, timer_id)


_timer_ref = TIMERPROC(_timer_proc)


def build_menu():
    hmenu = user32.CreatePopupMenu()
    for i, text in enumerate(('项目一', '项目二', '项目三'), start=1):
        user32.AppendMenuW(hmenu, 0, i, text)
    return hmenu


def analyze(img):
    pixels = list(img.convert('RGB').getdata())
    n = len(pixels)
    mean = tuple(round(sum(p[i] for p in pixels) / n) for i in range(3))
    brightness = 0.299 * mean[0] + 0.587 * mean[1] + 0.114 * mean[2]
    return mean, round(brightness)


def apply_window_theme_logged(hwnd, dark, theme_name):
    """手工对窗口应用主题并打印每一步返回值。theme_name 用于区分对照。"""
    ret_allow = allow_dark_for_window(hwnd, dark)
    if theme_name is None:
        hres = uxtheme.SetWindowTheme(hwnd, None, None)
    else:
        hres = uxtheme.SetWindowTheme(hwnd, theme_name, None)
    print(f'    [窗口 {hwnd:#x}] AllowDark={dark}→{ret_allow} '
          f'SetWindowTheme("{theme_name}")→HRESULT={hres & 0xFFFFFFFF:#x}')


def show_once(label, dark, owner_hwnd):
    print(f'--- {label}: 目标={"深" if dark else "浅"} ---')
    print(f'    ShouldAppsUseDarkMode={should_apps_use_dark_mode()}')
    ret = set_menu_dark_mode(dark)
    print(f'    set_menu_dark_mode→{ret} '
          f'ShouldAppsUseDarkMode={should_apps_use_dark_mode()}')

    hwnd0 = menu_hwnd()
    print(f'    显示前已存在菜单窗口: {hwnd0 and hex(hwnd0)}')
    theme_popup_menu_window(dark)          # 更新钩子目标 + 重刷已存在窗口
    if hwnd0:
        apply_window_theme_logged(hwnd0, dark,
                                  'DarkMode_Explorer' if dark else 'Explorer')
    destroyed = destroy_menu_window()
    hwnd1 = menu_hwnd()
    print(f'    destroy_menu_window→{destroyed}, 销毁后: {hwnd1 and hex(hwnd1)}')

    _capture.update(rect=None, shot=None, hwnd=None)
    hmenu = build_menu()
    user32.SetTimer(owner_hwnd, 1, 450, _timer_ref)
    shown = user32.TrackPopupMenuEx(
        hmenu, TPM_RIGHTALIGN | TPM_BOTTOMALIGN | TPM_RETURNCMD,
        300, 500, owner_hwnd, None)
    rect, shot, hwnd_menu = (_capture['rect'], _capture['shot'],
                             _capture['hwnd'])
    reused = (hwnd_menu == hwnd0) if (hwnd_menu and hwnd0) else None
    print(f'    显示中菜单窗口: {hwnd_menu and hex(hwnd_menu)} '
          f'(复用旧窗口={reused}) TrackPopupMenuEx→{shown}')
    if rect and shot is not None:
        crop = shot.crop(rect)
        crop.save(f'build/menuexp2_{label}.png')
        mean, brightness = analyze(crop)
        verdict = '深色' if brightness < 100 else '浅色'
        ok = (verdict == '深色') == dark
        print(f'    实测={verdict} 平均RGB={mean} 亮度={brightness} '
              f'矩形={rect} → {"✓ 正确" if ok else "✗ 不符"}')
    else:
        print(f'    菜单未捕获（rect={rect}）——判定无效')
    user32.DestroyMenu(hmenu)
    hwnd2 = menu_hwnd()
    print(f'    菜单关闭后窗口: {hwnd2 and hex(hwnd2)}')
    time.sleep(0.2)


def main():
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    hwnd = user32.CreateWindowExW(0, 'STATIC', 'CAuthMenuExp2',
                                  0x10000000, 0, 0, 0, 0, None, None, None, None)
    if not hwnd:
        print('无法创建宿主窗口')
        return
    print('=== 插桩实验：浅 → 深 → 浅（回归） ===')
    show_once('第1次_浅基线', False, hwnd)
    show_once('第2次_深', True, hwnd)
    show_once('第3次_浅_回归', False, hwnd)
    print('=== 实验结束 ===')


if __name__ == '__main__':
    main()
