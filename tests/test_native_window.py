"""native_window 子类化的真机冒烟测试（需要交互式会话）。

验证：样式位补齐（WS_CAPTION/WS_THICKFRAME）+ WM_NCCALCSIZE 被吞掉（返回 0）
——这是无边框窗口获得原生动画且不出现白边的两个关键点。
"""
import ctypes
import os
import unittest

from core.native_window import (
    enable_native_window_behaviors, WM_NCCALCSIZE, WS_CAPTION, WS_THICKFRAME,
)


@unittest.skipUnless(os.name == 'nt', 'Windows only')
class NativeWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.user32 = ctypes.WinDLL('user32')
        WS_POPUP, WS_VISIBLE = 0x80000000, 0x10000000
        cls.hwnd = cls.user32.CreateWindowExW(
            0, 'STATIC', 'CAuthNativeTest', WS_POPUP | WS_VISIBLE,
            10, 10, 120, 60, None, None, None, None)
        if not cls.hwnd:
            raise unittest.SkipTest('无法创建测试窗口')

    @classmethod
    def tearDownClass(cls):
        if cls.hwnd:
            cls.user32.DestroyWindow(cls.hwnd)

    def test_styles_added_and_nccalcsize_suppressed(self):
        self.assertTrue(enable_native_window_behaviors('CAuthNativeTest'))
        # 幂等：重复安装不报错
        self.assertTrue(enable_native_window_behaviors('CAuthNativeTest'))
        GWL_STYLE = -16
        style = self.user32.GetWindowLongW(self.hwnd, GWL_STYLE)
        self.assertTrue(style & WS_CAPTION)
        self.assertTrue(style & WS_THICKFRAME)
        # WM_NCCALCSIZE(wParam=TRUE) 返回 0：非客户区被吞掉，白边不再出现
        user32 = self.user32
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                        ctypes.c_size_t, ctypes.c_ssize_t]
        rect = (ctypes.c_long * 4)(10, 10, 130, 70)
        ret = user32.SendMessageW(self.hwnd, WM_NCCALCSIZE, 1,
                                  ctypes.addressof(rect))
        self.assertEqual(ret, 0)


if __name__ == '__main__':
    unittest.main()
