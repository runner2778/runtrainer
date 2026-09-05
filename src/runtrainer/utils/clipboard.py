"""Windows 剪贴板读取（Win32 API，无第三方依赖）。

pywebview/WebView2 默认不放开剪贴板粘贴，前端输入框粘贴（如 API Key）
通过本模块读系统剪贴板文本回填。

微信/富文本应用复制的内容可能不含 CF_UNICODETEXT（只有 HTML Format 或
延迟渲染），因此按 UNICODETEXT → ANSI TEXT → HTML 依次降级提取纯文本。
"""
from __future__ import annotations

import ctypes
import html
import logging
import re
import time

log = logging.getLogger(__name__)

CF_TEXT = 1
CF_UNICODETEXT = 13
CF_HTML = None  # 惰性注册

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
# HANDLE 返回值必须声明 c_void_p：64 位下默认 c_int 会把高 32 位截断成野指针
_user32.OpenClipboard.restype = ctypes.c_bool
_user32.CloseClipboard.restype = ctypes.c_bool
_user32.GetClipboardData.restype = ctypes.c_void_p
_user32.GetClipboardData.argtypes = [ctypes.c_uint]
_user32.EmptyClipboard.restype = ctypes.c_bool
_user32.RegisterClipboardFormatW.restype = ctypes.c_uint
_user32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalSize.restype = ctypes.c_size_t
_kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
_kernel32.GlobalUnlock.restype = ctypes.c_bool
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]

_OPEN_RETRIES = 5
_OPEN_RETRY_DELAY_S = 0.03  # 微信等程序复制后可能短暂占用剪贴板

_TAG_RE = re.compile(r"<[^>]+>")
_STYLE_SCRIPT_RE = re.compile(r"<(style|script)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)


def _html_format_id() -> int:
    global CF_HTML
    if CF_HTML is None:
        CF_HTML = _user32.RegisterClipboardFormatW("HTML Format")
    return CF_HTML


def _read_fmt(fmt: int) -> str | None:
    handle = _user32.GetClipboardData(fmt)
    if not handle:
        return None
    ptr = _kernel32.GlobalLock(handle)
    if not ptr:
        return None
    try:
        size = _kernel32.GlobalSize(handle)
        if not size:
            return None
        return ctypes.string_at(ptr, size)
    finally:
        _kernel32.GlobalUnlock(handle)


def _fmt_unicode(raw: bytes) -> str:
    # GlobalSize 包含 NUL 终止符，裁剪尾部（可能不止一个）
    return raw.decode("utf-16-le", errors="replace").rstrip("\x00")


def _fmt_ansi(raw: bytes) -> str:
    text = raw.rstrip(b"\x00")
    try:
        return text.decode("gbk", errors="replace")
    except LookupError:
        return text.decode("latin-1", errors="replace")


def _fmt_html_plain(raw: bytes) -> str:
    """HTML Format → 纯文本：按标准 StartFragment 标记切分，再去标签。"""
    text = raw.decode("utf-8", errors="replace").rstrip("\x00")
    if "<!--StartFragment-->" in text:
        text = text.split("<!--StartFragment-->")[-1]
    if "<!--EndFragment-->" in text:
        text = text.split("<!--EndFragment-->")[0]
    text = _STYLE_SCRIPT_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


def read_text() -> str | None:
    """读剪贴板文本；剪贴板为空/非文本/被占用时返回 None。

    降级顺序：CF_UNICODETEXT → CF_TEXT（ANSI）→ HTML Format 纯文本。
    """
    last_err = None
    for attempt in range(_OPEN_RETRIES):
        if _user32.OpenClipboard(None):
            break
        last_err = ctypes.get_last_error()
        if attempt < _OPEN_RETRIES - 1:
            time.sleep(_OPEN_RETRY_DELAY_S)
    else:
        log.debug(f"打开剪贴板失败（重试 {_OPEN_RETRIES} 次，err={last_err}）")
        return None
    try:
        raw = _read_fmt(CF_UNICODETEXT)
        if raw:
            return _fmt_unicode(raw)
        raw = _read_fmt(CF_TEXT)
        if raw:
            return _fmt_ansi(raw)
        raw = _read_fmt(_html_format_id())
        if raw:
            return _fmt_html_plain(raw)
        return None
    finally:
        _user32.CloseClipboard()
