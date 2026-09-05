"""剪贴板格式诊断：枚举所有格式 ID 与文本类格式内容预览。

用法：复制一段微信内容后运行本脚本。
.venv\\Scripts\\python tools\\probe_clipboard_formats.py
"""
import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.OpenClipboard.restype = ctypes.c_bool
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [ctypes.c_uint]
user32.EnumClipboardFormats.restype = ctypes.c_uint
user32.EnumClipboardFormats.argtypes = [ctypes.c_uint]
user32.GetClipboardFormatNameW.restype = ctypes.c_int
user32.GetClipboardFormatNameW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_int]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]

CF_TEXT = 1
CF_UNICODETEXT = 13
CF_HDROP = 15
CF_HTML = user32.RegisterClipboardFormatW("HTML Format")

if not user32.OpenClipboard(None):
    print("打开剪贴板失败（被占用）")
    sys.exit(1)
try:
    fmt = 0
    found = []
    while True:
        fmt = user32.EnumClipboardFormats(fmt)
        if fmt == 0:
            break
        name = ""
        buf = ctypes.create_unicode_buffer(128)
        if user32.GetClipboardFormatNameW(fmt, buf, 128):
            name = buf.value
        found.append((fmt, name))
    print(f"剪贴板格式: {found}")
    for fmt, name in found:
        if fmt not in (CF_TEXT, CF_UNICODETEXT, CF_HTML):
            continue
        h = user32.GetClipboardData(fmt)
        if not h:
            print(f"  fmt={fmt}({name}): 无数据")
            continue
        ptr = kernel32.GlobalLock(h)
        size = kernel32.GlobalSize(h)
        raw = ctypes.string_at(ptr, min(size, 4096))
        kernel32.GlobalUnlock(h)
        if fmt == CF_UNICODETEXT:
            txt = raw.decode("utf-16-le", errors="replace").rstrip("\x00")
        elif fmt == CF_TEXT:
            txt = raw.decode("gbk", errors="replace").rstrip("\x00")
        else:
            txt = raw.decode("utf-8", errors="replace")[:400]
        print(f"  fmt={fmt}({name}) size={size}: {txt[:200]!r}")
    # 对照：现有 read_text 的结果
    from runtrainer.utils import clipboard
    print(f"read_text() = {clipboard.read_text()!r}")
finally:
    user32.CloseClipboard()
