"""进程应用图标提取：exe 路径 → PNG base64 data URL。

纯 ctypes 实现（shell32/user32/gdi32），无第三方依赖：
  ExtractIconExW（取第一个图标，本机 SHGetFileInfoW 返回异常故不用）
  → GetIconInfo → GetDIBits(32bpp BGRA + 1bpp mask)
  → 手写 PNG 编码（zlib 压缩）→ base64 data URL

带进程级缓存：同一 exe 路径只提取一次；提取失败缓存 None，不反复重试。
"""
import base64
import ctypes
import logging
import struct
import threading
import zlib

logger = logging.getLogger('proc_icon')

SHGFI_ICON = 0x00000100
SHGFI_LARGEICON = 0x00000000
BI_RGB = 0
DIB_RGB_COLORS = 0


class _ICONINFO(ctypes.Structure):
    _fields_ = [
        ('fIcon', ctypes.c_int),
        ('xHotspot', ctypes.c_uint32),
        ('yHotspot', ctypes.c_uint32),
        ('hbmMask', ctypes.c_void_p),
        ('hbmColor', ctypes.c_void_p),
    ]


class _BITMAP(ctypes.Structure):
    _fields_ = [
        ('bmType', ctypes.c_long),
        ('bmWidth', ctypes.c_long),
        ('bmHeight', ctypes.c_long),
        ('bmWidthBytes', ctypes.c_long),
        ('bmPlanes', ctypes.c_uint16),
        ('bmBitsPixel', ctypes.c_uint16),
        ('bmBits', ctypes.c_void_p),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ('biSize', ctypes.c_uint32),
        ('biWidth', ctypes.c_long),
        ('biHeight', ctypes.c_long),
        ('biPlanes', ctypes.c_uint16),
        ('biBitCount', ctypes.c_uint16),
        ('biCompression', ctypes.c_uint32),
        ('biSizeImage', ctypes.c_uint32),
        ('biXPelsPerMeter', ctypes.c_long),
        ('biYPelsPerMeter', ctypes.c_long),
        ('biClrUsed', ctypes.c_uint32),
        ('biClrImportant', ctypes.c_uint32),
    ]


_shell32 = ctypes.windll.shell32
_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

_shell32.ExtractIconExW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                                    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.c_uint32]
_shell32.ExtractIconExW.restype = ctypes.c_uint32
_user32.GetIconInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ICONINFO)]
_user32.GetIconInfo.restype = ctypes.c_int
_user32.DestroyIcon.argtypes = [ctypes.c_void_p]
_user32.DestroyIcon.restype = ctypes.c_int
_user32.GetDC.argtypes = [ctypes.c_void_p]
_user32.GetDC.restype = ctypes.c_void_p
_user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_user32.ReleaseDC.restype = ctypes.c_int
_gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
_gdi32.GetObjectW.restype = ctypes.c_int
_gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
_gdi32.DeleteObject.restype = ctypes.c_int
_gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                             ctypes.c_void_p, ctypes.POINTER(_BITMAPINFOHEADER), ctypes.c_uint32]
_gdi32.GetDIBits.restype = ctypes.c_int

_cache = {}
_lock = threading.Lock()


def _png_chunk(tag, data):
    return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF)


def _encode_png(width, height, rgba_rows):
    """RGBA 逐行字节（每行前需加 filter byte 0）编码为最小化 PNG。"""
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw.extend(rgba_rows[y * stride:(y + 1) * stride])
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n'
            + _png_chunk(b'IHDR', ihdr)
            + _png_chunk(b'IDAT', zlib.compress(bytes(raw), 6))
            + _png_chunk(b'IEND', b''))


def _get_dib_bits(hdc, hbm, width, height, bit_count):
    """把 HBITMAP 指定位图读出为原始 DIB 字节（top-down）。"""
    bmi = _BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(bmi)
    bmi.biWidth = width
    bmi.biHeight = -height
    bmi.biPlanes = 1
    bmi.biBitCount = bit_count
    bmi.biCompression = BI_RGB
    row_bytes = ((width * bit_count + 31) // 32) * 4
    buf = ctypes.create_string_buffer(row_bytes * height)
    if not _gdi32.GetDIBits(hdc, hbm, 0, height, buf, ctypes.byref(bmi), DIB_RGB_COLORS):
        raise OSError('GetDIBits failed')
    return buf.raw


def _extract_icon_png(exe_path):
    large = ctypes.c_void_p()
    small = ctypes.c_void_p()
    n = _shell32.ExtractIconExW(exe_path, 0, ctypes.byref(large), ctypes.byref(small), 1)
    hicon = large.value or small.value
    if not n or not hicon:
        raise OSError('ExtractIconExW returned no icon')
    try:
        ii = _ICONINFO()
        if not _user32.GetIconInfo(hicon, ctypes.byref(ii)):
            raise OSError('GetIconInfo failed')
        try:
            if not ii.hbmColor:
                raise OSError('icon has no color bitmap')
            bm = _BITMAP()
            if not _gdi32.GetObjectW(ii.hbmColor, ctypes.sizeof(bm), ctypes.byref(bm)):
                raise OSError('GetObjectW(color) failed')
            w, h = bm.bmWidth, abs(bm.bmHeight)
            if not w or not h:
                raise OSError('empty icon bitmap')
            # 过大的图标（异常情况）截断到 32x32，避免 payload 失控
            if w > 32 or h > 32:
                w, h = 32, 32

            hdc = _user32.GetDC(None)
            try:
                color = _get_dib_bits(hdc, ii.hbmColor, w, h, 32)  # BGRA
                mask = None
                if ii.hbmMask:
                    try:
                        mask = _get_dib_bits(hdc, ii.hbmMask, w, h, 1)
                    except OSError:
                        mask = None
            finally:
                _user32.ReleaseDC(None, hdc)

            # BGRA → RGBA，并用 mask/alpha 推导透明度
            stride = w * 4
            mask_stride = ((w + 31) // 32) * 4
            has_alpha = any(color[i] for i in range(3, len(color), 4))
            rgba = bytearray(w * h * 4)
            for p in range(w * h):
                x = p % w
                y = p // w
                src = p * 4
                dst = p * 4
                rgba[dst] = color[src + 2]
                rgba[dst + 1] = color[src + 1]
                rgba[dst + 2] = color[src]
                if has_alpha:
                    rgba[dst + 3] = color[src + 3]
                elif mask:
                    byte = mask[y * mask_stride + (x >> 3)]
                    bit = (byte >> (7 - (x & 7))) & 1
                    rgba[dst + 3] = 0 if bit else 255
                else:
                    rgba[dst + 3] = 255
            return _encode_png(w, h, bytes(rgba))
        finally:
            if ii.hbmColor:
                _gdi32.DeleteObject(ii.hbmColor)
            if ii.hbmMask:
                _gdi32.DeleteObject(ii.hbmMask)
    finally:
        _user32.DestroyIcon(hicon)


def get_process_icon(exe_path):
    """返回 exe 的图标 data URL（data:image/png;base64,...）；失败/无路径返回 None。结果按路径缓存。"""
    if not exe_path:
        return None
    key = exe_path.lower()
    with _lock:
        if key in _cache:
            return _cache[key]
    try:
        png = _extract_icon_png(exe_path)
        url = 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')
    except Exception as e:
        logger.debug('extract icon failed for %s: %s', exe_path, e)
        url = None
    with _lock:
        _cache[key] = url
    return url


def clear_icon_cache():
    with _lock:
        _cache.clear()
