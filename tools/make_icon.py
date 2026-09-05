"""生成 SuperTrainer 应用图标（黑红主题：黑色圆角底 + 红色闪电）。

纯标准库（无 Pillow）：画 RGBA 位图 → PNG（zlib）→ 多尺寸 ICO。
用法：.venv\\Scripts\\python tools\\make_icon.py
"""
import struct
import sys
import zlib
from pathlib import Path

ACCENT = (224, 49, 49)     # --accent #e03131
BG = (11, 11, 14)          # --bg #0b0b0e
SIZE = 256
CORNER = 48                # 圆角半径
# 闪电多边形顶点（经典闪电，占画布主体）
BOLT = [
    (136, 12), (64, 152), (124, 152), (100, 244),
    (192, 104), (132, 104), (168, 12),
]


def in_poly(px, py, pts):
    """点在多边形内（射线法）。"""
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def in_rounded_rect(px, py):
    """点在圆角方形内（角为四分之一圆）。"""
    x, y = px, py
    if x < CORNER:
        cx, cy = CORNER, CORNER
        return not (y < CORNER and (x - cx) ** 2 + (y - cy) ** 2 > CORNER ** 2) \
           and not (y > SIZE - CORNER and (x - cx) ** 2 + (y - (SIZE - CORNER)) ** 2 > CORNER ** 2)
    if x > SIZE - CORNER:
        cx, cy = SIZE - CORNER, CORNER
        return not (y < CORNER and (x - cx) ** 2 + (y - cy) ** 2 > CORNER ** 2) \
           and not (y > SIZE - CORNER and (x - cx) ** 2 + (y - (SIZE - CORNER)) ** 2 > CORNER ** 2)
    return 0 <= y < SIZE


def render(size):
    """2x2 超采样渲染，返回 size×size 的 RGBA 行列表。"""
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            acc, n = [0, 0, 0], 0
            for sy in (0, 1):
                for sx in (0, 1):
                    px = (x + 0.25 + 0.5 * sx) * SIZE / size
                    py = (y + 0.25 + 0.5 * sy) * SIZE / size
                    if in_rounded_rect(px, py):
                        if in_poly(px, py, BOLT):
                            c = ACCENT
                        else:
                            c = BG
                        for k in range(3):
                            acc[k] += c[k]
                        n += 1
            if n == 0:
                row += b"\x00\x00\x00\x00"
            else:
                row += bytes([acc[0] // n, acc[1] // n, acc[2] // n, 255])
        rows.append(bytes(row))
    return rows


def png_chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def to_png(rows, size):
    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress(raw, 9))
            + png_chunk(b"IEND", b""))


def main():
    out = Path(__file__).resolve().parents[1] / "assets" / "supertrainer.ico"
    out.parent.mkdir(exist_ok=True)
    entries = b""
    imgs = []
    for size in (256, 64, 48, 32, 16):
        png = to_png(render(size), size)
        # PNG 条目：256 尺寸 w/h 记 0，其余记实际值
        wh = 0 if size == 256 else size
        entries += struct.pack("<BBBBHHII", wh, wh, 0, 0, 1, 32, len(png), 0)
        imgs.append(png)
    # 回填各图像数据偏移
    offset = 6 + 16 * len(imgs)
    full_entries = b""
    for i, img in enumerate(imgs):
        full_entries += entries[i * 16:i * 16 + 12] + struct.pack("<I", offset)
        offset += len(img)
    out.write_bytes(struct.pack("<HHH", 0, 1, len(imgs)) + full_entries + b"".join(imgs))
    print(f"已生成: {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    sys.exit(main())
