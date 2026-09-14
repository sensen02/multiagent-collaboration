#!/usr/bin/env python3
"""技能端口调用入口：把端口协议转给 run_3d.py（与 gpu / image-generation 同一约定）。

运行时的桥接契约是 `[sys.executable, invocation.bridge]`（**不带参数**），
而 stdin 在非交互 shell 里永远是一个打开的管道，靠"有没有数据"判断调用方式会误判，
所以这里显式声明：这个入口就是端口调用。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_3d  # noqa: E402

CAPABILITIES = {'kind': '3d_reconstruction',
                'provides': ['image_to_mesh', 'cuda_free_3d'],
                'batch': False,
                'device': 'cpu'}

if __name__ == '__main__':
    if '--capabilities' in sys.argv[1:]:
        print(json.dumps({'skill': 'theed', **CAPABILITIES}, ensure_ascii=False))
        raise SystemExit(0)
    raise SystemExit(run_3d.main(['--call']))
