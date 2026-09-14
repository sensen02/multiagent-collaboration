#!/usr/bin/env python3
"""把逐帧像素图拼成**动画精灵表**，并按契约写一份清单；也能反向切片。

与程序生成的像素素材（如 sword-demo 里用 PIL 画的 4 朝向 × 6 帧）不同，
这里处理的是**生成模型出的帧**：尺寸可能不一致、背景可能不透明、
面向顺序要靠人给。所以本脚本只做三件确定性的事，并把假设写进清单：

1. 统一每帧的网格尺寸（超出的按锚点裁，不足的补透明边）；
2. 按 `--rows 名字` × `--columns 名字` 的顺序拼表（顺序即语义，必须显式给）；
3. 写 `*.sheet.json`：格子尺寸、行列名、每帧文件名与哈希——**代码据此取值，不靠猜**。

反向（`--slice`）支持把一张大表切回逐帧，用于核对"生成的表是不是真的按行排的"。

用法：

    # 拼表：4 行（朝向）× 6 列（动作帧）
    python3 sprite_sheet.py --frames frames/*.png --rows down,left,right,up \
        --columns idle0,idle1,walk0,walk1,attack0,attack1 --out assets/sheet.png

    # 切片核对
    python3 sprite_sheet.py --slice assets/sheet.png --rows 4 --columns 6 --out-dir /tmp/frames
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pixelize import read_png, write_png          # 同一套 PNG 编解码，保持零依赖


def to_rgba(width, height, channels, pixels):
    if channels == 4:
        return pixels
    out = bytearray()
    for index in range(0, len(pixels), 3):
        out += bytes(pixels[index:index + 3]) + b'\xff'
    return bytes(out)


def canvas(width, height, fill=(0, 0, 0, 0)):
    return bytearray(bytes(fill) * (width * height))


def blit(target, target_w, target_h, source, source_w, source_h, offset_x, offset_y):
    for y in range(source_h):
        ty = y + offset_y
        if not (0 <= ty < target_h):
            continue
        row_src = y * source_w * 4
        row_dst = (ty * target_w + offset_x) * 4
        if offset_x < 0:
            start = -offset_x
            length = min(source_w - start, target_w)
            if length <= 0:
                continue
            target[row_dst:row_dst + length * 4] = source[row_src + start * 4:
                                                        row_src + (start + length) * 4]
        else:
            length = min(source_w, target_w - offset_x)
            if length <= 0:
                continue
            target[row_dst:row_dst + length * 4] = source[row_src:row_src + length * 4]


def fit_frame(path, cell_w, cell_h, anchor, files):
    width, height, channels, pixels = read_png(path)
    rgba = to_rgba(width, height, channels, pixels)
    if width == cell_w and height == cell_h:
        return rgba, {'width': width, 'height': height, 'fitted': 'exact'}
    if anchor == 'stretch':
        # 最近邻缩放：像素画必须用最近邻，双线性会糊
        scaled = canvas(cell_w, cell_h)
        for y in range(cell_h):
            sy = min(height - 1, y * height // cell_h)
            for x in range(cell_w):
                sx = min(width - 1, x * width // cell_w)
                dst = (y * cell_w + x) * 4
                src = (sy * width + sx) * 4
                scaled[dst:dst + 4] = rgba[src:src + 4]
        return bytes(scaled), {'width': width, 'height': height, 'fitted': 'nearest-stretch'}
    if anchor == 'center':
        ox, oy = (cell_w - width) // 2, (cell_h - height) // 2
    elif anchor == 'top':
        ox, oy = (cell_w - width) // 2, 0
    else:                                             # bottom：脚底对齐，适合角色
        ox, oy = (cell_w - width) // 2, cell_h - height
    box = canvas(cell_w, cell_h)
    blit(box, cell_w, cell_h, rgba, width, height, ox, oy)
    if width > cell_w or height > cell_h:
        files.append(f'{Path(path).name} 超出格子 {width}x{height} > {cell_w}x{cell_h}（按锚点裁剪）')
    return bytes(box), {'width': width, 'height': height, 'fitted': anchor, 'offset': [ox, oy]}


def build(frames, rows, columns, cell, out_path, anchor, json_path=None):
    cell_w, cell_h = cell
    expected = len(rows) * len(columns)
    if len(frames) != expected:
        raise ValueError(f'帧数 {len(frames)} 与 {len(rows)} 行 × {len(columns)} 列 = {expected} 不符；'
                         '顺序即语义，数目不对就不拼（宁可不做，不要拼出一张错表）')
    sheet_w, sheet_h = cell_w * len(columns), cell_h * len(rows)
    sheet = canvas(sheet_w, sheet_h)
    notes = []
    entries = []
    for index, path in enumerate(frames):
        row_index, column_index = divmod(index, len(columns))
        fitted, info = fit_frame(path, cell_w, cell_h, anchor, notes)
        blit(sheet, sheet_w, sheet_h, fitted, cell_w, cell_h,
             column_index * cell_w, row_index * cell_h)
        entries.append({'index': index, 'row': rows[row_index], 'column': columns[column_index],
                        'frame': columns[column_index], 'source': str(path),
                        'source_size': [info['width'], info['height']],
                        'fit': info['fitted'],
                        'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest()})
    write_png(out_path, sheet_w, sheet_h, bytes(sheet), 4)
    manifest = {
        'sheet': str(out_path), 'sheet_size': [sheet_w, sheet_h],
        'cell': [cell_w, cell_h], 'rows': rows, 'columns': columns,
        'cells': [len(columns), len(rows)], 'count': len(frames), 'anchor': anchor,
        'frames': entries, 'notes': notes,
        'contract': '第 r 行第 c 列的格子 = frames[r*len(columns)+c]；行列顺序即上面给出的顺序',
    }
    manifest_path = Path(json_path) if json_path else Path(out_path).with_suffix('.sheet.json')
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'ok': True, 'sheet': str(out_path), 'manifest': str(manifest_path),
            'size': [sheet_w, sheet_h], 'cell': [cell_w, cell_h], 'count': len(frames),
            'notes': notes, 'frames': entries}


def slice_sheet(path, rows, columns, out_dir, anchor_pad=0):
    """把一张表切回逐帧，用于核对生成的表是否真的按行列排布。"""
    width, height, channels, pixels = read_png(path)
    rgba = to_rgba(width, height, channels, pixels)
    if width % columns or height % rows:
        raise ValueError(f'表尺寸 {width}x{height} 不能被 {columns}x{rows} 整除；'
                         '要么表不是规则的，要么行列数给错了')
    cell_w, cell_h = width // columns, height // rows
    out_dir = Path(out_dir)
    written = []
    for r in range(rows):
        for c in range(columns):
            frame = canvas(cell_w, cell_h)
            for y in range(cell_h):
                src = ((r * cell_h + y) * width + c * cell_w) * 4
                dst = y * cell_w * 4
                frame[dst:dst + cell_w * 4] = rgba[src:src + cell_w * 4]
            target = out_dir / f'r{r:02d}_c{c:02d}_{cell_w}x{cell_h}.png'
            write_png(target, cell_w, cell_h, bytes(frame), 4)
            written.append(str(target))
    return {'ok': True, 'cell': [cell_w, cell_h], 'rows': rows, 'columns': columns, 'written': written}


def main(argv=None):
    parser = argparse.ArgumentParser(description='拼/切像素动画精灵表，并写契约清单')
    parser.add_argument('--frames', nargs='*', default=[], help='按 行优先 顺序给出的帧文件')
    parser.add_argument('--rows', default='', help='行名，逗号分隔（如 down,left,right,up）')
    parser.add_argument('--columns', default='', help='列名，逗号分隔（如 idle0,idle1,walk0...）')
    parser.add_argument('--cell', default='32x64', help='每格像素尺寸')
    parser.add_argument('--anchor', default='bottom', choices=['bottom', 'top', 'center', 'stretch'])
    parser.add_argument('--out', default='sheet.png')
    parser.add_argument('--json-path', default=None)
    parser.add_argument('--slice', default=None, help='反向：切一张已有的表')
    parser.add_argument('--out-dir', default='frames_out')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    try:
        if args.slice:
            result = slice_sheet(args.slice, int(args.rows.split(',')[0]) if args.rows else 0,
                                 int(args.columns.split(',')[0]) if args.columns else 0, args.out_dir)
        else:
            rows = [x.strip() for x in args.rows.split(',') if x.strip()] or ['row0']
            columns = [x.strip() for x in args.columns.split(',') if x.strip()] or ['col0']
            cell = tuple(int(x) for x in args.cell.lower().split('x'))
            result = build(args.frames, rows, columns, cell, args.out, args.anchor, args.json_path)
    except Exception as exc:                                       # noqa: BLE001
        print(json.dumps({'ok': False, 'error': f'{type(exc).__name__}: {exc}'}, ensure_ascii=False))
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        if 'sheet' in result:
            print(f"✓ {result['sheet']}  {result['size'][0]}x{result['size'][1]}  "
                  f"格子 {result['cell'][0]}x{result['cell'][1]}  {result['count']} 帧")
            for note in result.get('notes') or []:
                print(f"  ! {note}")
            print(f"  清单：{result['manifest']}")
        else:
            print(f"✓ 切出 {len(result['written'])} 帧到 {args.out_dir}，格子 {result['cell']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
