"""托盘菜单深色主题切换实验（诊断工具，可自动判定）。

在独立进程中完整复现应用链路：SetPreferredAppMode → WH_CALLWNDPROC
创建钩子 → 销毁旧菜单窗口 → TrackPopupMenu 显示。菜单显示期间由
同线程定时器回调定位菜单窗口真实矩形、全屏截图，然后 EndMenu 收起；
返回后按真实矩形裁剪分析亮度，自动判定浅色/深色。

不使用 SendInput（避免误触其他窗口）。运行时短暂闪现四次小菜单
（各约 0.7 秒，自动消失），裁剪截图保存到 build/menuexp_*.png。
"""
import ctypes
import logging
import time

from ctypes import wintypes
from PIL import ImageGrab

from core.native_theme import (
    destroy_menu_window, set_menu_dark_mode, theme_popup_menu_window,
    MENU_WINDOW_CLASS,
)

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

user32 = ctypes.WinDLL('user32')
user32.CreateWindowExW.restype = ctypes.c_void_p
user32.FindWindowW.restype = ctypes.c_void_p
user32.TrackPopupMenuEx.restype = ctypes.c_bool
user32.TrackPopupMenuEx.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_int, ctypes.c_int,
                                    ctypes.c_void_p, ctypes.c_void_p]
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint,
                            ctypes.c_void_p]

TPM_RIGHTALIGN = 0x0008
TPM_BOTTOMALIGN = 0x0020
TPM_RETURNCMD = 0x0100
WM_TIMER = 0x0113

TIMERPROC = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint,
                               ctypes.c_size_t, wintypes.DWORD)

# 每阶段由定时器回调填充：显示中的菜单窗口矩形 + 全屏截图
_capture = {'rect': None, 'shot': None}


class RECT(ctypes.Structure):
    _fields_ = [('left', wintypes.LONG), ('top', wintypes.LONG),
                ('right', wintypes.LONG), ('bottom', wintypes.LONG)]


def _timer_proc(hwnd, msg, timer_id, tick):
    """菜单模态循环内触发（同线程）：截全屏 → 记录菜单矩形 → EndMenu。"""
    try:
        hwnd_menu = user32.FindWindowW(MENU_WINDOW_CLASS, None)
        if hwnd_menu:
            rect = RECT()
            user32.GetWindowRect(hwnd_menu, ctypes.byref(rect))
            _capture['rect'] = (rect.left, rect.top, rect.right, rect.bottom)
        _capture['shot'] = ImageGrab.grab()  # 全屏，稍后按矩形裁剪
        user32.EndMenu()
    except Exception as exc:
        print(f'定时器回调异常: {exc}')
    finally:
        user32.KillTimer(hwnd, timer_id)


_timer_ref = TIMERPROC(_timer_proc)  # 防止回调被垃圾回收


def build_menu():
    hmenu = user32.CreatePopupMenu()
    for i, text in enumerate(('项目一', '项目二', '项目三'), start=1):
        user32.AppendMenuW(hmenu, 0, i, text)
    return hmenu


def analyze(img):
    pixels = list(img.convert('RGB').getdata())
    n = len(pixels)
    mean = tuple(round(sum(p[i] for p in pixels) / n) for i in range(3))
    variance = sum((p[0] - mean[0]) ** 2 for p in pixels) / n
    brightness = 0.299 * mean[0] + 0.587 * mean[1] + 0.114 * mean[2]
    return mean, round(brightness), round(variance ** 0.5)


def show_once(label, dark, owner_hwnd):
    """按当前主题设置显示一次菜单，显示期间自动截图并判定。"""
    set_menu_dark_mode(dark)
    theme_popup_menu_window(dark)   # 更新目标主题 + 创建钩子 + 重刷已存在窗口
    destroy_menu_window()           # 与应用 TrackPopupMenuEx 包装一致：显示前销毁
    _capture['rect'] = None
    _capture['shot'] = None
    hmenu = build_menu()
    user32.SetTimer(owner_hwnd, 1, 450, _timer_ref)
    shown = user32.TrackPopupMenuEx(
        hmenu, TPM_RIGHTALIGN | TPM_BOTTOMALIGN | TPM_RETURNCMD,
        300, 500, owner_hwnd, None)
    rect, shot = _capture['rect'], _capture['shot']
    if rect and shot is not None:
        crop = shot.crop(rect)
        crop.save(f'build/menuexp_{label}.png')
        mean, brightness, stddev = analyze(crop)
        verdict = '深色' if brightness < 100 else '浅色'
        ok = (verdict == '深色') == dark
        print(f'{label}: 期望={"深" if dark else "浅"} 实测={verdict} '
              f'平均RGB={mean} 亮度={brightness} 波动={stddev} '
              f'矩形={rect} 返回={shown} → {"✓ 正确" if ok else "✗ 不符"}')
    else:
        print(f'{label}: 菜单未捕获（shown={shown}，rect={rect}）——判定无效')
    user32.DestroyMenu(hmenu)
    time.sleep(0.2)


def main():
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    hwnd = user32.CreateWindowExW(0, 'STATIC', 'CAuthMenuExp',
                                  0x10000000, 0, 0, 0, 0, None, None, None, None)
    if not hwnd:
        print('无法创建宿主窗口')
        return
    print('=== 实验：浅 → 深 → 浅（回归） → 深，共四次 ===')
    show_once('第1次_浅基线', False, hwnd)
    show_once('第2次_深', True, hwnd)
    show_once('第3次_浅_回归', False, hwnd)
    show_once('第4次_深', True, hwnd)
    print('=== 实验结束 ===')


if __name__ == '__main__':
    main()
