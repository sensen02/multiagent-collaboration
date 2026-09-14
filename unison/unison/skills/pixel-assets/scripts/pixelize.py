#!/usr/bin/env python3
"""把任意生成图变成**真正的像素图**：目标网格 + 受限调色板 + 可选描边 + 最近邻预览。

为什么需要它（不是"加个像素风 LoRA 就完事"）：

扩散模型出的是 512×512 的"像素风插画"，像素**大小不均匀**、边缘有反锯齿、
颜色上千种——直接缩放当素材用，在任何放大倍数下都是糊的。
游戏素材要的是：**栅格确定**（每个像素对应固定 N×N 方块）、**调色板受限**（几十色以内）、
**透明背景干净**。这三件事是确定性问题，该用确定性代码做，不该指望模型。

流程：源图 → 面积平均降采样到目标网格 → 映射到调色板 → 去碎点 → 可选描边 →
输出 `*_px.png`（原生网格，1 像素 = 1 像素）+ `*_x{N}.png`（最近邻预览）+ 可核对的 JSON。

只用标准库，PNG 编解码自己实现（本机 `python3` 没有 Pillow 也能跑）。

用法：

    python3 pixelize.py in.png --grid 32x32 --palette db16 --out-dir px/
    python3 pixelize.py in.png --grid 16x16 --palette none --colors 12 --alpha-threshold 128
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path

# ── PNG 读写（8bit RGB/RGBA，非隔行）─────────────────────────────────────────

PNG_SIG = b'\x89PNG\r\n\x1a\n'
_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}


def read_png(path):
    data = Path(path).read_bytes()
    if data[:8] != PNG_SIG:
        raise ValueError(f'{path}: 不是 PNG（先转成 PNG 再来）')
    pos, width, height, bit_depth, color_type, idat = 8, None, None, None, None, bytearray()
    while pos + 8 <= len(data):
        (length,) = struct.unpack('>I', data[pos:pos + 4])
        chunk_type = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if chunk_type == b'IHDR':
            width, height, bit_depth, color_type = struct.unpack('>IIBB', chunk[:10])
        elif chunk_type == b'IDAT':
            idat += chunk
        elif chunk_type == b'IEND':
            break
    if bit_depth != 8 or color_type not in _CHANNELS:
        raise ValueError(f'只支持 8bit RGB/RGBA PNG（当前 bit_depth={bit_depth} color_type={color_type}）')
    channels = _CHANNELS[color_type]
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray()
    previous = bytearray(stride)
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        offset += 1
        line = bytearray(raw[offset:offset + stride])
        offset += stride
        if filter_type == 1:
            for x in range(channels, stride):
                line[x] = (line[x] + line[x - channels]) & 255
        elif filter_type == 2:
            for x in range(stride):
                line[x] = (line[x] + previous[x]) & 255
        elif filter_type == 3:
            for x in range(stride):
                left = line[x - channels] if x >= channels else 0
                line[x] = (line[x] + ((left + previous[x]) >> 1)) & 255
        elif filter_type == 4:
            for x in range(stride):
                left = line[x - channels] if x >= channels else 0
                up = previous[x]
                up_left = previous[x - channels] if x >= channels else 0
                estimate = left + up - up_left
                da, db, dc = abs(estimate - left), abs(estimate - up), abs(estimate - up_left)
                pred = left if (da <= db and da <= dc) else (up if db <= dc else up_left)
                line[x] = (line[x] + pred) & 255
        out += line
        previous = line
    return width, height, channels, bytes(out)


def write_png(path, width, height, pixels, channels):
    """pixels 为 RGB 或 RGBA 的 bytes（每行 width*channels）。"""
    color_type = 6 if channels == 4 else 2
    raw = bytearray()
    stride = width * channels
    for y in range(height):
        raw.append(0)                                  # filter: none
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(tag, payload):
        body = tag + payload
        return struct.pack('>I', len(payload)) + body + struct.pack('>I', zlib.crc32(body) & 0xffffffff)

    out = bytearray(PNG_SIG)
    out += chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, color_type, 0, 0, 0))
    out += chunk(b'IDAT', zlib.compress(bytes(raw), 9))
    out += chunk(b'IEND', b'')
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(bytes(out))


# ── 调色板 ────────────────────────────────────────────────────────────────────

PALETTES = {
    # DawnBringer 16（像素画通用）
    'db16': [(20, 12, 28), (68, 36, 52), (48, 52, 109), (78, 74, 78), (133, 76, 48),
             (52, 101, 36), (208, 70, 72), (117, 113, 97), (89, 125, 206), (210, 125, 44),
             (133, 149, 161), (109, 170, 44), (210, 170, 153), (109, 194, 202), (218, 212, 94),
             (222, 238, 214)],
    # DawnBringer 32
    'db32': [(0, 0, 0), (34, 32, 52), (69, 40, 60), (102, 57, 49), (143, 86, 59), (223, 113, 38),
             (217, 160, 102), (238, 195, 154), (251, 242, 54), (153, 229, 80), (106, 190, 48),
             (55, 148, 110), (75, 105, 47), (82, 75, 36), (50, 60, 57), (63, 125, 90),
             (38, 43, 68), (43, 90, 133), (61, 120, 168), (101, 169, 208), (140, 193, 217),
             (208, 227, 238), (255, 255, 255), (190, 163, 175), (139, 100, 125), (94, 54, 97),
             (50, 28, 74), (109, 60, 96), (158, 69, 103), (208, 89, 118), (231, 138, 146),
             (241, 190, 170)],
    # PICO-8
    'pico8': [(0, 0, 0), (29, 43, 83), (126, 37, 83), (0, 135, 81), (171, 82, 54), (95, 87, 79),
              (194, 195, 199), (255, 241, 232), (255, 0, 77), (255, 163, 0), (255, 236, 39),
              (0, 228, 54), (41, 173, 255), (131, 118, 156), (255, 119, 168), (255, 204, 170)],
    # Game Boy（4 级绿）
    'gameboy': [(15, 56, 15), (48, 98, 48), (139, 172, 15), (155, 188, 15)],
    # NES 近似（常用子集）
    'nes': [(0, 0, 0), (252, 252, 252), (188, 188, 188), (124, 124, 124), (164, 0, 0), (248, 56, 0),
            (188, 0, 0), (228, 92, 16), (248, 184, 0), (252, 152, 56), (0, 120, 0), (88, 216, 84),
            (0, 88, 0), (0, 0, 168), (0, 120, 248), (104, 68, 252), (216, 0, 204), (248, 120, 248)],
    # 本机测试用的 1bit 双色
    'bw': [(0, 0, 0), (255, 255, 255)],
}


def median_cut(colors, k):
    """median-cut 生成 k 色调色板（输入是 (count, (r,g,b)) 列表）。"""
    boxes = [list(colors)]
    while len(boxes) < k:
        # 选"体积最大"的盒子来切：像素数 × 最长边，避免把大块均匀区域切碎
        box = max(boxes, key=lambda b: sum(c[0] for c in b) * (
            max(max(c[1][i] for c in b) - min(c[1][i] for c in b) for i in range(3))))
        if len(box) < 2:
            break
        boxes.remove(box)
        channel = max(range(3), key=lambda i: max(c[1][i] for c in box) - min(c[1][i] for c in box))
        box.sort(key=lambda c: c[1][channel])
        total = sum(c[0] for c in box)
        acc = 0
        split = 1
        for index, item in enumerate(box):
            acc += item[0]
            if acc >= total / 2:
                split = max(1, min(len(box) - 1, index))
                break
        boxes.append(box[:split])
        boxes.append(box[split:])
    palette = []
    for box in boxes:
        total = sum(c[0] for c in box) or 1
        palette.append(tuple(round(sum(c[1][i] * c[0] for c in box) / total) for i in range(3)))
    return palette


def nearest(color, palette):
    best, best_distance = palette[0], None
    for candidate in palette:
        distance = sum((color[i] - candidate[i]) ** 2 for i in range(3))
        if best_distance is None or distance < best_distance:
            best, best_distance = candidate, distance
    return best


# ── 主流程 ────────────────────────────────────────────────────────────────────

def downscale(width, height, channels, pixels, grid_w, grid_h, alpha_threshold):
    """面积平均降采样：源图按 grid 分块，块内对**不透明**像素求均值（避免黑边渗色）。"""
    out = bytearray()
    for gy in range(grid_h):
        y0, y1 = gy * height // grid_h, max(gy * height // grid_h + 1, (gy + 1) * height // grid_h)
        for gx in range(grid_w):
            x0, x1 = gx * width // grid_w, max(gx * width // grid_w + 1, (gx + 1) * width // grid_w)
            r = g = b = a = 0
            count = 0
            for y in range(y0, min(y1, height)):
                row = y * width * channels
                for x in range(x0, min(x1, width)):
                    base = row + x * channels
                    if channels == 4 and pixels[base + 3] < alpha_threshold:
                        a += pixels[base + 3]
                        continue
                    r += pixels[base]
                    g += pixels[base + 1]
                    b += pixels[base + 2]
                    a += pixels[base + 3] if channels == 4 else 255
                    count += 1
            total = max(1, (min(x1, width) - x0) * (min(y1, height) - y0))
            if count == 0:
                out += bytes((0, 0, 0, 0))
            else:
                out += bytes((r // count, g // count, b // count, a // total))
    return bytes(out)


def despeckle(grid_w, grid_h, pixels, minimum_neighbors=2):
    """去掉孤立的碎点：一个不透明像素若邻居里同类太少，就换成邻居中出现最多的颜色。"""
    def at(x, y):
        base = (y * grid_w + x) * 4
        return tuple(pixels[base:base + 4])

    out = bytearray(pixels)
    for y in range(grid_h):
        for x in range(grid_w):
            current = at(x, y)
            if current[3] == 0:
                continue
            same = 0
            counts = {}
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = x + dx, y + dy
                    if not (0 <= nx < grid_w and 0 <= ny < grid_h):
                        continue
                    neighbor = at(nx, ny)
                    if neighbor[3] == 0:
                        continue
                    if neighbor == current:
                        same += 1
                    counts[neighbor] = counts.get(neighbor, 0) + 1
            if same < minimum_neighbors and counts:
                replacement = max(counts.items(), key=lambda item: item[1])[0]
                base = (y * grid_w + x) * 4
                out[base:base + 4] = bytes(replacement)
    return bytes(out)


def outline(grid_w, grid_h, pixels, color=(0, 0, 0, 255)):
    """给不透明区域加一圈 1 像素描边（像素画常见做法），背景保持透明。"""
    def opaque(x, y):
        if not (0 <= x < grid_w and 0 <= y < grid_h):
            return False
        return pixels[(y * grid_w + x) * 4 + 3] > 0

    out = bytearray(pixels)
    for y in range(grid_h):
        for x in range(grid_w):
            if opaque(x, y):
                continue
            if any(opaque(x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)):
                base = (y * grid_w + x) * 4
                out[base:base + 4] = bytes(color)
    return bytes(out)


def upscale_nearest(grid_w, grid_h, pixels, factor):
    out = bytearray()
    for y in range(grid_h):
        row = bytearray()
        for x in range(grid_w):
            base = (y * grid_w + x) * 4
            row += bytes(pixels[base:base + 4]) * factor
        out += row * factor
    return bytes(out), grid_w * factor, grid_h * factor


def parse_grid(text):
    parts = str(text).lower().replace('×', 'x').split('x')
    if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
        raise ValueError(f'--grid 形如 32x32，收到 {text!r}')
    return int(parts[0]), int(parts[1])


def pixelize(path, grid, palette_name, colors, alpha_threshold, do_outline, preview_factor,
             out_dir, despeckle_min=2):
    width, height, channels, pixels = read_png(path)
    grid_w, grid_h = grid
    small = downscale(width, height, channels, pixels, grid_w, grid_h, alpha_threshold)

    # 调色板：固定用内置，auto 用 median-cut，none 表示保留抗锯齿原色（不推荐）
    transparent = []
    histogram = {}
    for index in range(0, len(small), 4):
        r, g, b, a = small[index:index + 4]
        if a < alpha_threshold:
            transparent.append(index)
            continue
        histogram[(r, g, b)] = histogram.get((r, g, b), 0) + 1
    if palette_name == 'auto':
        palette = median_cut([(c, k) for k, c in histogram.items()], colors)
    elif palette_name == 'none':
        palette = None
    else:
        palette = PALETTES[palette_name]

    mapped = bytearray(small)
    if palette is not None:
        cache = {}
        for index in range(0, len(mapped), 4):
            r, g, b, a = mapped[index:index + 4]
            if a < alpha_threshold:
                mapped[index:index + 4] = bytes((0, 0, 0, 0))
                continue
            key = (r, g, b)
            hit = cache.get(key)
            if hit is None:
                hit = nearest(key, palette)
                cache[key] = hit
            mapped[index:index + 4] = bytes((hit[0], hit[1], hit[2], 255))

    if despeckle_min:
        mapped = bytearray(despeckle(grid_w, grid_h, bytes(mapped), despeckle_min))
    if do_outline:
        mapped = bytearray(outline(grid_w, grid_h, bytes(mapped)))

    stem = Path(path).stem
    out_dir = Path(out_dir)
    px_path = out_dir / f'{stem}_px{grid_w}x{grid_h}.png'
    write_png(px_path, grid_w, grid_h, bytes(mapped), 4)
    preview_path = None
    if preview_factor and preview_factor > 1:
        big, big_w, big_h = upscale_nearest(grid_w, grid_h, bytes(mapped), preview_factor)
        preview_path = out_dir / f'{stem}_px{grid_w}x{grid_h}_x{preview_factor}.png'
        write_png(preview_path, big_w, big_h, big, 4)

    used = {}
    for index in range(0, len(mapped), 4):
        if mapped[index + 3] == 0:
            continue
        key = tuple(mapped[index:index + 3])
        used[key] = used.get(key, 0) + 1
    record = {
        'source': str(path), 'source_size': [width, height],
        'grid': [grid_w, grid_h], 'palette': palette_name,
        'palette_size': len(palette) if palette else None,
        'colors_used': len(used), 'transparent_pixels': sum(1 for i in range(0, len(mapped), 4)
                                                            if mapped[i + 3] == 0),
        'outline': bool(do_outline), 'despeckle_min_neighbors': despeckle_min,
        'output': str(px_path), 'preview': str(preview_path) if preview_path else None,
        'scale_ratio': [round(width / grid_w, 2), round(height / grid_h, 2)],
    }
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description='把生成图变成真正的像素图（目标网格 + 受限调色板）')
    parser.add_argument('images', nargs='+')
    parser.add_argument('--grid', default='32x32', help='目标像素网格，如 32x32 / 16x24')
    parser.add_argument('--palette', default='auto',
                        help='内置调色板名（db16/db32/pico8/gameboy/nes/bw）、auto 或 none')
    parser.add_argument('--colors', type=int, default=16, help='--palette auto 时的目标色数')
    parser.add_argument('--alpha-threshold', type=int, default=128)
    parser.add_argument('--outline', action='store_true', help='给不透明区域加 1 像素描边')
    parser.add_argument('--preview-factor', type=int, default=8, help='最近邻预览放大倍数，0 表示不生成')
    parser.add_argument('--out-dir', default='.')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    if args.palette != 'auto' and args.palette != 'none' and args.palette not in PALETTES:
        print(json.dumps({'ok': False, 'error': f'未知调色板 {args.palette!r}；'
                                                f'可用：{", ".join(sorted(PALETTES))}, auto, none'},
                         ensure_ascii=False))
        return 1
    grid = parse_grid(args.grid)
    records = []
    failed = 0
    for path in args.images:
        try:
            records.append(pixelize(path, grid, args.palette, args.colors, args.alpha_threshold,
                                    args.outline, args.preview_factor, args.out_dir))
        except Exception as exc:                                   # noqa: BLE001
            failed += 1
            records.append({'source': path, 'error': f'{type(exc).__name__}: {exc}'})
    envelope = {'ok': failed == 0, 'grid': list(grid), 'palette': args.palette,
                'items': records, 'failed': failed}
    if args.json:
        print(json.dumps(envelope, ensure_ascii=False, indent=1))
    else:
        for item in records:
            if item.get('error'):
                print(f"× {Path(item['source']).name}: {item['error']}")
            else:
                print(f"✓ {Path(item['source']).name} -> {Path(item['output']).name} "
                      f"网格={item['grid'][0]}x{item['grid'][1]} 用色={item['colors_used']} "
                      f"缩放比={item['scale_ratio']}")
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
