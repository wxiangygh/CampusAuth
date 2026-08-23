"""Windows DPAPI helpers for protecting credentials at rest."""
from __future__ import annotations

import base64
import ctypes
import logging
import os
from ctypes import wintypes

logger = logging.getLogger('wifi_tray')
PREFIX = 'dpapi:'


class DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD),
                ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def protect_text(value: str) -> str:
    if not value or value.startswith(PREFIX) or os.name != 'nt':
        return value
    raw = value.encode('utf-8')
    input_blob, input_buffer = _blob(raw)
    output_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptProtectData(ctypes.byref(input_blob), 'CampusAuth', None, None, None,
                                    0, ctypes.byref(output_blob)):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return PREFIX + base64.b64encode(encrypted).decode('ascii')
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def unprotect_text(value: str) -> str:
    if not value or not value.startswith(PREFIX) or os.name != 'nt':
        return value
    encrypted = base64.b64decode(value[len(PREFIX):], validate=True)
    input_blob, input_buffer = _blob(encrypted)
    output_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptUnprotectData(ctypes.byref(input_blob), None, None, None, None,
                                      0, ctypes.byref(output_blob)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode('utf-8')
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)
