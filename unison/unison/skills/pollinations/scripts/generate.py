#!/usr/bin/env python3
"""Pollinations.AI 图像生成客户端与独立脚本。

纯标准库实现：零第三方依赖。
支持 CLI 直接调用与模块导入。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"
POLLINATIONS_MODELS = "https://image.pollinations.ai/models"
USER_AGENT = "Unison/0.1 (skill: pollinations)"
MAX_BYTES = 32 * 1024 * 1024
DEFAULT_TIMEOUT = 120.0

KNOWN_MODELS = [
    "flux",
    "flux-realism",
    "flux-cablyai",
    "flux-anime",
    "flux-3d",
    "turbo",
]


def sniff(data: bytes):
    """按魔数识别真实格式，返回 (格式, 宽, 高)；无法识别时格式为 None。"""
    if not data:
        return None, None, None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return "png", width, height
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg", *_jpeg_size(data)
    if data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return "gif", width, height
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return "webp", width, height
        if chunk == b"VP8 " and len(data) >= 30:
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return "webp", width, height
        if chunk == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            return "webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None, None, None


def _jpeg_size(data: bytes):
    """遍历 JPEG 段获取真实尺寸。"""
    index = 2
    total = len(data)
    while index + 9 < total:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in {0xD8, 0x01} or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xD9:
            break
        length = struct.unpack(">H", data[index + 2:index + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in {0xC4, 0xC8, 0xCC}:
            height, width = struct.unpack(">HH", data[index + 5:index + 9])
            return width, height
        index += 2 + length
    return None, None


def _slug(text: str, limit: int = 50) -> str:
    value = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", str(text or "").strip()).strip("-").lower()
    return value[:limit] or "pollinations"


def build_url(prompt: str, width: int = 1024, height: int = 1024, seed: int | None = None,
              model: str = "", nologo: bool = True) -> str:
    encoded_prompt = urllib.parse.quote(prompt.strip())
    params = {}
    if width:
        params["width"] = str(int(width))
    if height:
        params["height"] = str(int(height))
    if seed is not None:
        params["seed"] = str(int(seed))
    if model:
        params["model"] = str(model).strip()
    if nologo:
        params["nologo"] = "true"
    query = urllib.parse.urlencode(params)
    return f"{POLLINATIONS_BASE}/{encoded_prompt}?{query}" if query else f"{POLLINATIONS_BASE}/{encoded_prompt}"


def fetch_image(prompt: str, width: int = 1024, height: int = 1024, seed: int | None = None,
                model: str = "", timeout: float = DEFAULT_TIMEOUT) -> bytes:
    url = build_url(prompt, width, height, seed, model)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise RuntimeError(f"响应内容超过大小上限 {MAX_BYTES} 字节")
            return body
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(512).decode("utf-8", "replace").strip()
        except Exception:
            pass
        raise RuntimeError(f"HTTP 错误 {exc.code}: {detail or exc.reason}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络连接失败: {exc.reason}") from None


def render(prompt: str, out_path: str, width: int = 1024, height: int = 1024,
           seed: int | None = None, model: str = "", timeout: float = DEFAULT_TIMEOUT,
           workspace: str | None = None) -> dict:
    if not prompt or not str(prompt).strip():
        raise ValueError("缺少 prompt 参数")

    # 处理输出路径
    if not out_path:
        out_path = f"{_slug(prompt)}_{int(time.time())}.jpg"
    if workspace and not os.path.isabs(out_path):
        target_path = os.path.normpath(os.path.join(workspace, out_path))
    else:
        target_path = os.path.abspath(out_path)

    os.makedirs(os.path.dirname(target_path), exist_ok=True)

    t0 = time.time()
    data = fetch_image(prompt, width=width, height=height, seed=seed, model=model, timeout=timeout)
    cost = round(time.time() - t0, 2)

    fmt, real_w, real_h = sniff(data)
    if fmt is None:
        raise RuntimeError(f"返回数据不是有效的图片格式 (响应前 32 字节: {data[:32]!r})")

    # 如果文件名未带合适扩展名且不是自动生成的，可保留；如果是目录则自动补充扩展名
    if os.path.isdir(target_path):
        target_path = os.path.join(target_path, f"{_slug(prompt)}_{int(time.time())}.{fmt}")

    with open(target_path, "wb") as f:
        f.write(data)

    return {
        "ok": True,
        "path": target_path,
        "format": fmt,
        "width": real_w,
        "height": real_h,
        "bytes": len(data),
        "prompt": prompt,
        "model": model or "default",
        "seed": seed,
        "time_seconds": cost,
        "url": build_url(prompt, width, height, seed, model),
    }


def list_models(timeout: float = 10.0) -> list[str]:
    req = urllib.request.Request(POLLINATIONS_MODELS, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return KNOWN_MODELS


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Pollinations.AI 免费云端图像生成客户端")
    parser.add_argument("--prompt", "-p", help="生成提示词（推荐英文描写主体、环境、构图）")
    parser.add_argument("--out", "-o", help="输出图片文件路径")
    parser.add_argument("--width", "-w", type=int, default=1024, help="图片宽度（默认 1024）")
    parser.add_argument("--height", "-H", type=int, default=1024, help="图片高度（默认 1024）")
    parser.add_argument("--seed", "-s", type=int, default=None, help="随机种子（整数，同种子同提示词可复现）")
    parser.add_argument("--model", "-m", default="", help="指定模型（例如 flux, turbo）")
    parser.add_argument("--timeout", "-t", type=float, default=DEFAULT_TIMEOUT, help="请求超时时间（秒）")
    parser.add_argument("--list-models", action="store_true", help="列出当前可用模型")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.list_models:
        models = list_models(args.timeout)
        print(json.dumps({"models": models}, ensure_ascii=False))
        return 0
    if not args.prompt:
        print(json.dumps({"ok": False, "error": "缺少 --prompt 参数"}, ensure_ascii=False))
        return 1
    try:
        res = render(
            prompt=args.prompt,
            out_path=args.out or f"./pollinations_{int(time.time())}.jpg",
            width=args.width,
            height=args.height,
            seed=args.seed,
            model=args.model,
            timeout=args.timeout,
        )
        print(json.dumps(res, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
