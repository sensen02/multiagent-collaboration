#!/usr/bin/env python3
"""把 CUDA-only 的 `torchmcubes` 换成纯 CPU 实现（**不改 TripoSR 一行代码**）。

为什么需要它（A 卡真机踩过）：

- TripoSR 的 `tsr/models/isosurface.py` 第一行就是 `from torchmcubes import marching_cubes`；
- `torchmcubes` 是带 CUDA 的 C++ 扩展，在 ROCm/CPU 环境下 **pip 装不上**
  （实测：`Encountered error while generating package metadata`，连 wheel 都构建不出来）；
- 于是整个 pipeline 在**第一行 import** 就死，跟显卡算力毫无关系——**这不是性能问题，是依赖问题**。

修法不是"改上游代码"，而是**在导入路径上替换模块**：`sitecustomize.py` 会被
Python 自动导入，在这里把 `torchmcubes` 注册进 `sys.modules`，于是 `import torchmcubes`
拿到的是我们这份 CPU 实现。上游文件一个字节都不用动，升级 TripoSR 也不会冲突。

接口必须严格对齐 `torchmcubes.marching_cubes(volume, thresh)`：

- 输入 `volume`：任意维度的 torch 张量（会被转成 3D）；
- 输出 `(vertices, faces)`：**vertices 用体素索引坐标**（0..N-1，不是归一化的 0..1），
  这样才能和 `isosurface.py` 里的 `v_pos / (self.resolution - 1.0)` 对上；
- `faces` 必须是 int64 且 dtype/形状能被 `trimesh` 直接吃。

实现用 `skimage.measure.marching_cubes`（纯 C，pip 有 CPU wheel）。
它返回的顶点是 voxel 坐标、面是 int32——正好就是我们要的形状。

用法（`run.py` 之前设一次即可）：

    export PYTHONPATH=/srv/unison-assets/bin/torchmcubes_shim:$PYTHONPATH
"""
from __future__ import annotations
import numpy as np

try:
    from skimage import measure as _measure
except Exception:                                              # noqa: BLE001
    _measure = None

_SHIM_VERSION = '1.0-cpu'
_SHIM_BACKEND = 'skimage' if _measure is not None else None


def marching_cubes(volume, thresh=0.0, **kwargs):
    """与 torchmcubes.marching_cubes 同形的 CPU 实现。"""
    if _measure is None:
        raise RuntimeError('marching cubes 不可用：需要 scikit-image（pip install scikit-image）')
    import torch

    array = volume.detach().cpu().numpy() if hasattr(volume, 'detach') else np.asarray(volume)
    array = np.ascontiguousarray(array.astype(np.float32, copy=False))
    if array.ndim != 3:
        raise ValueError(f'需要 3D 体素，收到 {array.ndim} 维')

    spacing = kwargs.pop('spacing', (1.0, 1.0, 1.0))
    if not np.isfinite(array).all():
        array = np.nan_to_num(array, nan=float(np.nanmin(array[np.isfinite(array)]) if np.isfinite(array).any() else 0.0))

    # 全在阈值一侧时没有任何表面：返回空网格而不是抛错（上游会把空网格写成空 obj）
    if array.min() > thresh or array.max() < thresh:
        return torch.zeros((0, 3), dtype=torch.float32), torch.zeros((0, 3), dtype=torch.int64)

    try:
        vertices, faces, _, _ = _measure.marching_cubes(array, level=float(thresh), spacing=spacing)
    except (ValueError, RuntimeError) as exc:
        # skimage 在"体素里没有跨越阈值的格子"时抛错，语义上就是空表面
        if 'level' in str(exc) or 'No surface' in str(exc) or 'surface' in str(exc).lower():
            return torch.zeros((0, 3), dtype=torch.float32), torch.zeros((0, 3), dtype=torch.int64)
        raise

    # skimage 返回的数组可能是负步长视图（内部翻转轴），torch.as_tensor 会直接拒绝，
    # 所以先 copy 成连续内存再交给 torch。
    vertices = np.ascontiguousarray(vertices, dtype=np.float32)
    faces = np.ascontiguousarray(faces, dtype=np.int64)
    return (torch.as_tensor(vertices, dtype=torch.float32),
            torch.as_tensor(faces, dtype=torch.int64))


def __getattr__(name):
    """其它符号（如 `marching_cubes_func`）按缺省处理：明确报错，不假装支持。"""
    raise AttributeError(f'torchmcubes_shim 只实现了 marching_cubes；{name!r} 未实现（CPU shim）')
