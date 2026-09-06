"""Win32 弹出菜单深色主题（uxtheme 未公开接口 + WH_CALLWNDPROC 创建钩子）。

原生托盘菜单（TrackPopupMenu）的配色由 Windows 系统主题决定，应用侧
没有公开接口控制。成熟实现（adzm/win32-darkmode，Electron/Chromium
托盘菜单同源机制）：

1. SetPreferredAppMode（ordinal 135，Win10 1903+）：进程级深/浅偏好；
   配合 RefreshImmersiveColorPolicyState（ordinal 104）刷新 uxtheme 的
   颜色策略状态。注意深→浅必须用 ForceLight(3) 而非 Default(0)：
   Default 表示"跟随系统"，实测无法撤销此前的 ForceDark（菜单回不到
   浅色，表现为"只能成功切换一次"），ForceLight 与 ForceDark 同级，
   才能强制翻转；
2. FlushMenuThemes（ordinal 136）：SetPreferredAppMode 切换后必须调用，
   否则 uxtheme 的菜单配色表保持进程级缓存不刷新（表现为"只能成功
   切换一次"）；
3. WH_CALLWNDPROC 线程钩子捕获菜单窗口（类名 #32768）的 WM_CREATE，
   在创建瞬间执行 AllowDarkModeForWindow（ordinal 133）+ SetWindowTheme
   （深色 DarkMode_Explorer / 浅色显式 Explorer，注意该函数导出自
   uxtheme.dll 而非 user32）——逐窗口双保险；
4. 主题切换时立即对已存在的菜单窗口重刷，不依赖下一次显示。

老系统缺少对应导出函数时静默降级（菜单保持跟随系统主题）。
"""
import logging
import os
import threading
import ctypes

logger = logging.getLogger('wifi_tray')

if os.name == 'nt':
    _user32 = ctypes.WinDLL('user32')
    _uxtheme = ctypes.WinDLL('uxtheme')
    _user32.FindWindowW.restype = ctypes.c_void_p
    _user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.SetWindowsHookExW.restype = ctypes.c_void_p
    _user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                          ctypes.c_void_p, ctypes.c_uint32]
    _user32.CallNextHookEx.restype = ctypes.c_ssize_t
    _user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                       ctypes.c_size_t, ctypes.c_ssize_t]
    _uxtheme.SetWindowTheme.restype = ctypes.c_long  # HRESULT
    _uxtheme.SetWindowTheme.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                        ctypes.c_wchar_p]
else:
    _user32 = None
    _uxtheme = None

PREFERRED_APP_MODE_DEFAULT = 0
PREFERRED_APP_MODE_FORCE_DARK = 2
PREFERRED_APP_MODE_FORCE_LIGHT = 3
MENU_WINDOW_CLASS = '#32768'
WM_CREATE = 0x0001
HC_ACTION = 0
_WH_CALLWNDPROC = 4

_hook = {'dark': False}
_hooks = {}  # thread_id -> (handle, callback)；线程钩子只对安装它的线程生效
_hook_lock = threading.Lock()


def _get_uxtheme():
    import ctypes
    kernel32 = ctypes.WinDLL('kernel32')
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetProcAddress.restype = ctypes.c_void_p
    kernel32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    handle = kernel32.GetModuleHandleW('uxtheme')
    if not handle:
        return None
    return kernel32, handle


def set_menu_dark_mode(dark: bool) -> bool:
    """设置进程内 Win32 弹出菜单的深色渲染偏好，返回是否成功应用。

    dark=True 强制深色（ForceDark）；dark=False 强制浅色（ForceLight）。
    不能用 Default（跟随系统）回退：Default 压不过此前的 ForceDark。
    切换后必须依次调用 FlushMenuThemes（ordinal 136，清空菜单配色表
    缓存——缺了它菜单配色就粘在上一次的模式上）和
    RefreshImmersiveColorPolicyState（ordinal 104，刷新颜色策略状态）。
    """
    if os.name != 'nt' or _user32 is None:
        return False
    try:
        kernel32, handle = _get_uxtheme()
        if not handle:
            return False
        set_preferred_app_mode = kernel32.GetProcAddress(handle, ctypes.c_void_p(135))
        flush_menu_themes = kernel32.GetProcAddress(handle, ctypes.c_void_p(136))
        refresh_policy = kernel32.GetProcAddress(handle, ctypes.c_void_p(104))
        if not set_preferred_app_mode:
            logger.debug('set_menu_dark_mode: SetPreferredAppMode 不可用（老系统），跳过')
            return False
        mode_fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int)
        ctypes.cast(set_preferred_app_mode, mode_fn)(
            PREFERRED_APP_MODE_FORCE_DARK if dark else PREFERRED_APP_MODE_FORCE_LIGHT)
        if flush_menu_themes:
            flush_fn = ctypes.WINFUNCTYPE(None)
            ctypes.cast(flush_menu_themes, flush_fn)()
        if refresh_policy:
            refresh_fn = ctypes.WINFUNCTYPE(ctypes.c_long)
            ctypes.cast(refresh_policy, refresh_fn)()
        return True
    except Exception as exc:
        logger.debug('set_menu_dark_mode failed: %s', exc)
        return False


def _apply_menu_window_theme(hwnd, dark: bool) -> None:
    """对单个菜单窗口应用深/浅主题：先 AllowDarkModeForWindow 打标记，
    再 SetWindowTheme 设置主题类（顺序敏感）。

    浅色必须显式设 'Explorer'：uxtheme 的菜单颜色表在进程首次深色
    菜单后会缓存深色配色，即使 PreferredAppMode 已回浅（实测
    ShouldAppsUseDarkMode=False）也不重读；显式窗口主题类才能覆盖。
    注意 SetWindowTheme 导出自 uxtheme.dll（曾误从 user32 取而静默
    失败，导致逐窗口主题从未生效）。
    """
    if _uxtheme is None:
        return
    try:
        kernel32, handle = _get_uxtheme()
        proc = kernel32.GetProcAddress(handle, ctypes.c_void_p(133)) \
            if handle else None  # AllowDarkModeForWindow
        if proc:
            allow = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                       ctypes.c_long)
            ctypes.cast(proc, allow)(hwnd, 1 if dark else 0)
        _uxtheme.SetWindowTheme(hwnd, 'DarkMode_Explorer' if dark
                                else 'Explorer', None)
    except Exception as exc:
        logger.debug('_apply_menu_window_theme failed: %s', exc)


def _get_class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _maybe_theme_new_window(hwnd) -> None:
    """WM_CREATE 时调用：菜单窗口（#32768）则在创建瞬间应用当前主题。"""
    try:
        if _get_class_name(hwnd).casefold() == MENU_WINDOW_CLASS.casefold():
            _apply_menu_window_theme(hwnd, _hook['dark'])
            logger.info('[tray] 菜单窗口已创建，应用主题（dark=%s）', _hook['dark'])
    except Exception:
        pass  # 钩子路径绝不抛异常


def destroy_menu_window() -> bool:
    """销毁已存在的弹出菜单窗口（须由其所属线程调用），返回是否销毁。

    菜单窗口被系统按线程复用，其主题状态有滞后性；显示前主动销毁，
    让 TrackPopupMenu 以当前主题重新创建（创建钩子会在创建瞬间
    应用主题），彻底消除"只能成功切换一次"的生命周期不确定性。
    """
    if os.name != 'nt' or _user32 is None:
        return False
    try:
        user32 = ctypes.WinDLL('user32')
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.DestroyWindow.argtypes = [ctypes.c_void_p]
        hwnd = user32.FindWindowW(MENU_WINDOW_CLASS, None)
        if not hwnd:
            return False
        if user32.DestroyWindow(hwnd):
            logger.info('[tray] 已销毁旧菜单窗口（将以当前主题重建）')
            return True
        return False
    except Exception as exc:
        logger.debug('destroy_menu_window failed: %s', exc)
        return False


def _callwndproc(n_code, wparam, lparam):
    """WH_CALLWNDPROC 钩子：捕获发送到本线程窗口的 WM_CREATE。"""
    try:
        if n_code == HC_ACTION and lparam:
            class CWPSTRUCT(ctypes.Structure):
                _fields_ = [('lParam', ctypes.c_ssize_t),
                            ('wParam', ctypes.c_size_t),
                            ('message', ctypes.c_uint),
                            ('hwnd', ctypes.c_void_p)]
            cwp = CWPSTRUCT.from_address(lparam)
            if cwp.message == WM_CREATE and cwp.hwnd:
                _maybe_theme_new_window(cwp.hwnd)
    except Exception:
        pass  # 钩子回调绝不抛异常
    return _user32.CallNextHookEx(None, n_code, wparam, lparam)


def theme_popup_menu_window(dark: bool) -> None:
    """同步弹出菜单主题：更新目标主题、确保创建钩子就绪、重刷已存在窗口。

    主题切换（apply_tray_theme）与显示前（TrackPopupMenuEx 包装）都会
    调用本函数；首次显示由创建钩子覆盖。
    """
    if os.name != 'nt' or _user32 is None:
        return
    _hook['dark'] = bool(dark)
    _ensure_menu_create_hook()
    try:
        user32 = ctypes.WinDLL('user32')
        user32.FindWindowW.restype = ctypes.c_void_p
        hwnd = user32.FindWindowW(MENU_WINDOW_CLASS, None)
        if hwnd:
            _apply_menu_window_theme(hwnd, _hook['dark'])
    except Exception as exc:
        logger.debug('theme_popup_menu_window failed: %s', exc)


def _ensure_menu_create_hook():
    """在当前线程安装 WH_CALLWNDPROC 线程钩子（按线程 id 管理，幂等）。

    线程钩子只对安装它的线程生效——菜单窗口在调用 TrackPopupMenu 的
    线程（pystray 图标线程）上创建，显示前包装器在该线程触发安装。
    """
    with _hook_lock:
        import ctypes
        kernel32 = ctypes.WinDLL('kernel32')
        kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
        tid = kernel32.GetCurrentThreadId()
        if _hooks.get(tid):
            return True
        try:
            HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                          ctypes.c_size_t, ctypes.c_ssize_t)
            callback = HOOKPROC(_callwndproc)
            handle = _user32.SetWindowsHookExW(_WH_CALLWNDPROC, callback,
                                               None, tid)
            if not handle:
                logger.debug('menu create hook install failed (tid=%s)', tid)
                return False
            _hooks[tid] = (handle, callback)
            logger.info('[tray] 菜单窗口创建钩子已安装（线程 %s）', tid)
            return True
        except Exception as exc:
            logger.debug('menu create hook install failed: %s', exc)
            return False
