#!/usr/bin/env python3
"""技能端口调用入口：把端口协议转给 probe.py。

运行时的桥接契约是 `[sys.executable, invocation.bridge]`（**不带参数**），而 stdin 在
非交互 shell 里永远是一个打开的管道，靠"有没有数据"判断调用方式会误判。所以这里显式声明：
这个入口就是端口调用，probe.py 保持"直接运行 → 人类可读报告"。

`--capabilities` 让**执行体自报它实现了什么**，供调用方（人或模型）直接问答，
不需要去读脚本源码才能知道这一档存不存在。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe  # noqa: E402

# 本技能是"探测 + 判别"，不是生成器（生成在 image-generation 的 local 后端）。
CAPABILITIES = {'kind': 'probe',
                'backends': ['vulkan', 'rocm', 'cuda', 'cpu'],
                'provides': ['gpu_probe', 'weight_role_check', 'output_degeneracy_check'],
                'batch': False,
                'scripts': ['probe.py', 'weights.py', 'imagecheck.py']}

if __name__ == '__main__':
    if '--capabilities' in sys.argv[1:]:
        print(json.dumps({'skill': 'gpu', **CAPABILITIES}, ensure_ascii=False))
        raise SystemExit(0)
    raise SystemExit(probe.main(['--call']))
