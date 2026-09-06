"""native_theme（托盘菜单深色适配）测试：WH_CALLWNDPROC 创建钩子。"""
import ctypes
import os
import unittest
from unittest.mock import patch

from core.native_theme import (
    _callwndproc, _hooks, _hook,
    destroy_menu_window, set_menu_dark_mode, theme_popup_menu_window,
)


@unittest.skipUnless(os.name == 'nt', 'Windows only')
class NativeThemeSmokeTests(unittest.TestCase):
    def test_set_menu_dark_mode_toggles(self):
        # uxtheme ordinal 135（SetPreferredAppMode）+ 104（刷新颜色策略）可用
        self.assertTrue(set_menu_dark_mode(True))
        self.assertTrue(set_menu_dark_mode(False))

    def test_theme_popup_menu_window_no_window_is_silent(self):
        # 无已创建的菜单窗口（#32768）时静默跳过，不抛异常
        theme_popup_menu_window(True)
        theme_popup_menu_window(False)

    def test_destroy_menu_window_without_menu_returns_false(self):
        # 测试进程内没有菜单窗口：返回 False 且不抛异常
        self.assertFalse(destroy_menu_window())

    def test_create_hook_installs_on_current_thread(self):
        from core.native_theme import _ensure_menu_create_hook
        self.assertTrue(_ensure_menu_create_hook())
        import ctypes
        kernel32 = ctypes.WinDLL('kernel32')
        kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
        self.assertTrue(_hooks.get(kernel32.GetCurrentThreadId()) is not None)

    def test_callwndproc_themes_menu_window_on_wm_create(self):
        """WM_CREATE + 菜单窗口类名 → 触发主题应用。"""
        user32 = ctypes.WinDLL('user32')
        hwnd = user32.CreateWindowExW(
            0, 'STATIC', 'CAuthThemeTest', 0x10000000 | 0x80000000,
            10, 10, 80, 40, None, None, None, None)
        self.addCleanup(lambda: user32.DestroyWindow(hwnd) if hwnd else None)
        if not hwnd:
            self.skipTest('无法创建测试窗口')

        class CWPSTRUCT(ctypes.Structure):
            _fields_ = [('lParam', ctypes.c_ssize_t),
                        ('wParam', ctypes.c_size_t),
                        ('message', ctypes.c_uint),
                        ('hwnd', ctypes.c_void_p)]

        with patch('core.native_theme._maybe_theme_new_window') as spy:
            # WM_CREATE → 应调用主题应用
            cwp = CWPSTRUCT(0, 0, 0x0001, hwnd)
            _callwndproc(0, 0, ctypes.addressof(cwp))
            spy.assert_called_once_with(hwnd)
            # 其他消息 → 不触发
            spy.reset_mock()
            cwp = CWPSTRUCT(0, 0, 0x0002, hwnd)
            _callwndproc(0, 0, ctypes.addressof(cwp))
            spy.assert_not_called()

    def test_maybe_theme_new_window_ignores_non_menu_class(self):
        """非菜单类（STATIC）不应用主题。"""
        user32 = ctypes.WinDLL('user32')
        hwnd = user32.CreateWindowExW(
            0, 'STATIC', 'CAuthThemeTest2', 0x10000000 | 0x80000000,
            10, 10, 80, 40, None, None, None, None)
        self.addCleanup(lambda: user32.DestroyWindow(hwnd) if hwnd else None)
        if not hwnd:
            self.skipTest('无法创建测试窗口')
        applied = []
        with patch('core.native_theme._apply_menu_window_theme',
                   side_effect=lambda h, d: applied.append((h, d))):
            from core.native_theme import _maybe_theme_new_window
            _maybe_theme_new_window(hwnd)
        self.assertEqual(applied, [])

    def test_apply_menu_window_theme_uses_explicit_theme_classes(self):
        """浅色必须显式设 'Explorer'（而非 NULL）：uxtheme 的菜单配色表
        在进程首次深色菜单后缓存，只有显式窗口主题类才能覆盖回浅。
        SetWindowTheme 导出自 uxtheme.dll（曾误取 user32 静默失败）。"""
        import types
        from core.native_theme import _apply_menu_window_theme
        calls = []
        fake = types.SimpleNamespace(
            SetWindowTheme=lambda h, t, s: (calls.append((h, t)), 0)[1])
        with patch('core.native_theme._uxtheme', fake),              patch('core.native_theme._get_uxtheme', return_value=(None, None)):
            _apply_menu_window_theme(0x1234, True)
            _apply_menu_window_theme(0x1234, False)
        self.assertEqual(calls, [(0x1234, 'DarkMode_Explorer'),
                                 (0x1234, 'Explorer')])

    def test_light_mode_uses_force_light_not_default(self):
        """深→浅必须用 ForceLight(3)：Default(0) 压不过之前的 ForceDark。"""
        from core import native_theme
        self.assertEqual(native_theme.PREFERRED_APP_MODE_FORCE_LIGHT, 3)
        self.assertNotEqual(native_theme.PREFERRED_APP_MODE_FORCE_LIGHT,
                            native_theme.PREFERRED_APP_MODE_DEFAULT)


if __name__ == '__main__':
    unittest.main()
