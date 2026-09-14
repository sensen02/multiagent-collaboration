"""让 `import torchmcubes` 自动落到 CPU shim 上——不用改 TripoSR 一行代码。

Python 启动时会自动导入 `sitecustomize`（只要它在 sys.path 上），
所以把这个目录放进 `PYTHONPATH` 就等于"在导入路径上装了个转接头"：

    PYTHONPATH=<此目录>:$PYTHONPATH python3 <TripoSR>/run.py input.png --device cpu ...

为什么不在 TripoSR 里改 import：上游代码是第三方源码，改了就会被下一次 `git pull` 冲掉；
而"依赖在 A 卡上装不上"是**环境问题**，就该在环境层解决。
"""
import importlib.util
import sys
from pathlib import Path

_TARGET = 'torchmcubes'
_HERE = Path(__file__).resolve().parent


def _install():
    if _TARGET in sys.modules:
        return
    real = importlib.util.find_spec(_TARGET) if importlib.util.find_spec else None
    if real is not None:
        return                                   # 真货存在就不动它
    spec = importlib.util.spec_from_file_location(_TARGET, _HERE / 'torchmcubes.py')
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[_TARGET] = module
    spec.loader.exec_module(module)


_install()
