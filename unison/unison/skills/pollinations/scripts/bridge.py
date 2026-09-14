#!/usr/bin/env python3
"""Pollinations 技能端口桥接器 (invocation.kind: bridge)。

支持两种调用模式：
1. 探针模式：`bridge.py --capabilities` 输出能力声明 JSON。
2. 桥接模式：标准输入读取 Unison 运行时的 JSON 请求，标准输出返回统一契约的 JSON。
   - 单次调用：返回 {"ok": true, "result": {...}}
   - 批量调用：返回 {"ok": true, "results": [{...}, ...]}
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import generate

CAPABILITIES = {
    "skill": "pollinations",
    "backends": ["pollinations"],
    "provides": ["cloud_image_gen", "free_image_api"],
    "batch": True,
}


def render_item(item: dict, workspace: str) -> dict:
    prompt = item.get("prompt")
    if not prompt or not str(prompt).strip():
        raise ValueError("单项调用缺少 prompt")

    out_path = item.get("out") or item.get("path")
    width = item.get("width", 1024)
    height = item.get("height", 1024)
    seed = item.get("seed")
    model = item.get("model", "")
    timeout = item.get("timeout", generate.DEFAULT_TIMEOUT)

    return generate.render(
        prompt=str(prompt),
        out_path=out_path,
        width=width,
        height=height,
        seed=seed,
        model=model,
        timeout=timeout,
        workspace=workspace,
    )


def handle_payload(payload: dict) -> dict:
    workspace = payload.get("workspace") or os.getcwd()
    args = payload.get("args")

    if isinstance(args, list):
        # 批量调用
        results = []
        for index, item in enumerate(args):
            if not isinstance(item, dict):
                raise ValueError(f"args[{index}] 必须是 JSON 对象")
            res = render_item(item, workspace)
            results.append(res)
        return {"ok": True, "results": results}
    elif isinstance(args, dict):
        # 单次调用
        res = render_item(args, workspace)
        return {"ok": True, "result": res}
    else:
        raise ValueError("args 必须是对象或对象数组")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pollinations Skill Bridge")
    parser.add_argument("--capabilities", action="store_true", help="输出能力元数据")
    args, _ = parser.parse_known_args(argv)

    if args.capabilities:
        print(json.dumps(CAPABILITIES, ensure_ascii=False))
        return 0

    # 运行时从 stdin 管道传入 JSON
    raw = sys.stdin.read().strip()
    if not raw:
        # 如果既没传 --capabilities 也没传 stdin，输出帮助
        parser.print_help(sys.stderr)
        return 1

    try:
        payload = json.loads(raw)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"输入不是有效的 JSON: {exc}"}, ensure_ascii=False))
        return 1

    try:
        response = handle_payload(payload)
        print(json.dumps(response, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
