#!/usr/bin/env python3
"""量一张 PNG 的画面统计量，供模型判断它是不是废图。

真机踩过的坑（2026-09-13 实测两次，其中一次是"像素风立绘全是灰的"）：
生图命令**退出码 0、文件也写出来了、尺寸也对**，但内容是**大面积单一底色**
——实测最常见颜色占 **73–76%** 的像素，取值恰好是 `128,121,11x`
（零潜变量解码出来的那种灰）。
"文件存在、PNG、尺寸正确"这三条**证明不了它不是废图**，
而报告里最容易出现的假证据恰好就是这三条。

本脚本只做**测量**，不下结论——这里**没有** `degenerate` 标记、没有 `verdict` 判词：
"这张图能不能用"由模型与人来判断，脚本只把可复核的数字交出来。

| 指标 | 含义 |
|---|---|
| `dominant_fraction` | 抽样里最常见颜色的占比 |
| `dominant_color` | 那个最常见颜色的 RGB |
| `local_contrast` | 相邻像素平均绝对差（0–1） |
| `block_std` | 8×8 块均值之后的标准差（0–1） |
| `distinct_colors` | 抽样色数 |
| `repeated_columns` | 与左邻列几乎逐字节相同的列占比 |

实测参考（2026-09-13 晚，RDNA4 + sd-cli master-859，两组各 4–8 张；**只是参考，
不是阈值**）：

|          | 主色占比    | 抽样色数    | 块std       |
|----------|------------|------------|-------------|
| 零潜变量废图 | 0.73–0.81  | 1632–8178  | 0.024–0.035 |
| 正常出图   | 0.06–0.90  | 3630–13415 | 0.096–0.408 |

注意这组数字本身说明：**主色占比与色数都区分不了坏图**（坏图的噪点让色数上万、
主色也高），两档之间唯一有明显间隔的是块标准差。因此判断时必须把几个量合起来看，
不要只看其中一个——更要紧的是：**别把区间当成规则**，先看数字再自己下结论。

只解析 PNG（标准库，无需 Pillow）；JPEG/WebP 给明确提示而不是假装支持。

用法：
    python3 imagecheck.py out.png [more.png ...]
    python3 imagecheck.py --json out.png
"""
from __future__ import annotations
import argparse
import json
import struct
import zlib
from pathlib import Path

PNG_SIG = b'\x89PNG\r\n\x1a\n'
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def read_png(path):
    """返回 (width, height, channels, pixels:bytes, error)。"""
    try:
        data = Path(path).read_bytes()
    except Exception as exc:                                    # noqa: BLE001
        return None, None, None, None, f'read-error:{exc}'
    if data[:8] != PNG_SIG:
        return None, None, None, None, 'not-png'
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
    if not width or color_type not in _CHANNELS:
        return None, None, None, None, f'unsupported-png:color_type={color_type}'
    if bit_depth != 8:
        return width, height, _CHANNELS[color_type], None, f'unsupported-bit-depth:{bit_depth}'
    channels = _CHANNELS[color_type]
    try:
        raw = zlib.decompress(bytes(idat))
    except Exception as exc:                                    # noqa: BLE001
        return width, height, channels, None, f'zlib-error:{exc}'

    stride = width * channels
    out = bytearray()
    previous = bytearray(stride)
    offset = 0
    try:
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
    except IndexError:
        return width, height, channels, None, 'truncated-pixel-data'
    return width, height, channels, bytes(out), None


def analyse(path):
    width, height, channels, pixels, error = read_png(path)
    report = {'path': str(path), 'width': width, 'height': height, 'channels': channels,
              'error': error}
    if error and pixels is None:
        # 解码失败也是事实，由 error 报告；不恢复已经移除的质量判词字段。
        return report

    # 透明像素不算内容：带 alpha 的素材（游戏精灵图）大片区域本就是空的，
    # 把它们算进"主色/结构"会把一张正常的透明素材误判成"大片单色"。
    # 实测：hero-move.png 透明区被当成 (0,0,0) 后主色占比 90%，几乎被误杀。
    alpha_aware = channels == 4
    report['alpha_aware'] = alpha_aware

    samples = []
    step = channels * (4 if alpha_aware else 7)
    for index in range(0, len(pixels) - 3, step):
        if alpha_aware and pixels[index + 3] <= 8:
            continue
        samples.append((pixels[index], pixels[index + 1], pixels[index + 2]))
    total = max(1, len(samples))
    report['sampled_pixels'] = len(samples)
    histogram = {}
    for color in samples:
        histogram[color] = histogram.get(color, 0) + 1
    dominant, dominant_count = max(histogram.items(), key=lambda item: item[1])
    report['dominant_color'] = list(dominant)
    report['dominant_fraction'] = round(dominant_count / total, 3)
    report['distinct_colors'] = len(histogram)

    gradient = 0
    count = 0
    for y in range(height):
        row = y * width * channels
        for x in range(1, width, 3):
            i = row + x * channels
            j = i - channels
            if alpha_aware and (pixels[i + 3] <= 8 or pixels[j + 3] <= 8):
                continue
            gradient += abs(pixels[i] - pixels[j]) + abs(pixels[i + 1] - pixels[j + 1]) \
                + abs(pixels[i + 2] - pixels[j + 2])
            count += 3
    report['local_contrast'] = round(gradient / max(1, count) / 255, 4)

    block = 8
    means = []
    for by in range(0, max(1, height - block + 1), block):
        for bx in range(0, max(1, width - block + 1), block):
            total_value = 0
            samples_in_block = 0
            for y in range(by, min(by + block, height), 2):
                row = y * width * channels
                for x in range(bx, min(bx + block, width), 2):
                    i = row + x * channels
                    total_value += pixels[i] + pixels[i + 1] + pixels[i + 2]
                    samples_in_block += 3
            if samples_in_block:
                means.append(total_value / samples_in_block)
    if means:
        mean = sum(means) / len(means)
        variance = sum((value - mean) ** 2 for value in means) / len(means)
        report['block_std'] = round((variance ** 0.5) / 255, 4)
    else:
        report['block_std'] = None

    repeated = 0
    for x in range(1, width):
        difference = 0
        checked = 0
        for y in range(0, height, 4):
            i = (y * width + x) * channels
            j = i - channels
            difference += abs(pixels[i] - pixels[j]) + abs(pixels[i + 1] - pixels[j + 1]) \
                + abs(pixels[i + 2] - pixels[j + 2])
            checked += 3
        if difference / max(1, checked) < 2:
            repeated += 1
    report['repeated_columns'] = round(repeated / max(1, width - 1), 3)

    # 到此为止全部是**测量**。这里不产出 degenerate / verdict 这类判词：
    # 数字摆出来，判断留给模型与人（见文件头：区间只是参考，不是规则）。
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='只读测量生成图的画面统计量（不下结论）')
    parser.add_argument('paths', nargs='+')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    reports = [analyse(p) for p in args.paths]
    if args.json:
        print(json.dumps({'ok': True, 'images': reports}, ensure_ascii=False, indent=1))
        return 0
    for report in reports:
        name = Path(report['path']).name
        if report.get('error'):
            print(f"{name}: 无法判读：{report['error']}（JPEG/WebP 请用能解码它的工具）")
            continue
        print(f"{name}: {report['width']}x{report['height']} ch={report['channels']} "
              f"主色={tuple(report.get('dominant_color') or ())} "
              f"主色占比={report.get('dominant_fraction')} 色数={report.get('distinct_colors')} "
              f"对比度={report.get('local_contrast')} 块std={report.get('block_std')} "
              f"重复列={report.get('repeated_columns')}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
