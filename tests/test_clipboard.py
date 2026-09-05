"""剪贴板读取（Windows Win32 API）：往返 + 微信/富文本降级链。"""
import ctypes
import subprocess
import sys

import pytest

from runtrainer.utils import clipboard

GMEM_MOVEABLE = 0x0002
CF_TEXT = 1
CF_UNICODETEXT = 13

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.OpenClipboard.restype = ctypes.c_bool
_user32.EmptyClipboard.restype = ctypes.c_bool
_user32.CloseClipboard.restype = ctypes.c_bool
_user32.SetClipboardData.restype = ctypes.c_void_p
_user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
_kernel32.GlobalAlloc.restype = ctypes.c_void_p
_kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalUnlock.restype = ctypes.c_bool
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]

_pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Win32 API 专用")


def set_clipboard(fmt, data: bytes):
    assert _user32.OpenClipboard(None)
    _user32.EmptyClipboard()
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data) + 2)
    assert handle
    ptr = _kernel32.GlobalLock(handle)
    assert ptr
    ctypes.memmove(ptr, data, len(data))
    ctypes.memset(ptr + len(data), 0, 2)  # NUL 终止
    _kernel32.GlobalUnlock(handle)
    assert _user32.SetClipboardData(fmt, handle)
    _user32.CloseClipboard()
    # handle 由系统接管，不 GlobalFree


def test_clipboard_roundtrip():
    """写入系统剪贴板后 read_text 应原样读回（无野指针、无尾部 NUL）。"""
    payload = "sk-test-1234567890abcdef"
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Set-Clipboard -Value '{payload}'"],
        check=True, capture_output=True)
    assert clipboard.read_text() == payload


def test_ansi_only_fallback():
    """微信/旧程序只放 CF_TEXT（ANSI/GBK）时也能读。"""
    payload = "sk-密钥测试-123"
    set_clipboard(CF_TEXT, payload.encode("gbk"))
    assert clipboard.read_text() == payload


def test_html_only_fallback():
    """只放 HTML Format 的富文本应用 → 提取纯文本。"""
    body = "跑步计划：间歇 6x800m"
    cf_html = (
        "Version:0.9\r\n"
        "StartHTML:97\r\n"
        "EndHTML:200\r\n"
        "StartFragment:150\r\n"
        "EndFragment:186\r\n"
        "<html><body><p>开头忽略</p><!--StartFragment-->"
        f"{body}<!--EndFragment--><p>结尾忽略</p></body></html>"
    )
    set_clipboard(ctypes.windll.user32.RegisterClipboardFormatW("HTML Format"),
                  cf_html.encode("utf-8"))
    assert clipboard.read_text() == body


def test_html_fragment_offset_broken_still_extracts():
    """CF_HTML 头部偏移损坏时退回标记切分。"""
    body = "健康数据 120 天"
    cf_html = ("Version:0.9\r\nStartHTML:0\r\nEndHTML:0\r\nStartFragment:99999\r\n"
               f"EndFragment:99999\r\n<html><body><!--StartFragment-->{body}"
               "<!--EndFragment--></body></html>")
    set_clipboard(ctypes.windll.user32.RegisterClipboardFormatW("HTML Format"),
                  cf_html.encode("utf-8"))
    assert clipboard.read_text() == body


def test_empty_clipboard_returns_none():
    assert _user32.OpenClipboard(None)
    _user32.EmptyClipboard()
    _user32.CloseClipboard()
    assert clipboard.read_text() is None


def test_unicode_beats_ansi_when_both_present():
    """UNICODETEXT 优先于 ANSI。"""
    set_clipboard(CF_UNICODETEXT, "sk-unicode-优先".encode("utf-16-le"))
    assert clipboard.read_text() == "sk-unicode-优先"
