#!/usr/bin/env python3
"""跑齐 tests/ 下的全部套件。

**为什么需要它**：`tests/test_runtime.py` 与 `tests/test_models.py` 定义了 TestCase 类却漏了
`unittest.main()`，于是 `python3 tests/test_runtime.py` 会**静默退出 0**——看起来"通过"，
实际上一个用例都没跑。运行时最核心的 72 个用例就这样长期没被执行过。

unittest 的 `discover` 也不够用（`tests/` 不是可导入包，且它只按模式收集）。
所以这里逐个文件加载模块、直接驱动 discovery，把每个文件的用例数与失败数都打出来，
漏跑会立刻显形。

用法：
    python3 tests/run_all.py            # 全部
    python3 tests/run_all.py runtime    # 只跑文件名含 runtime 的

退出码：有任何失败/错误/加载失败即 1，否则 0。
"""
from __future__ import annotations

import importlib.util
import io
import sys
import traceback
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None):
    patterns = [a for a in (argv if argv is not None else sys.argv[1:]) if not a.startswith('-')]
    files = sorted((ROOT / 'tests').glob('test_*.py'))
    if patterns:
        files = [f for f in files if any(p in f.name for p in patterns)]
    if not files:
        print('没有匹配的测试文件')
        return 1

    total = failures = errors = skipped = 0
    broken: list[str] = []
    for path in files:
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        buf = io.StringIO()
        try:
            with redirect_stdout(buf), redirect_stderr(buf):
                spec.loader.exec_module(module)
            suite = unittest.TestLoader().loadTestsFromModule(module)
            count = suite.countTestCases()
            if count == 0:
                # 没有用例是一种缺陷，不是"通过"：说明这个文件里的测试根本没被发现。
                broken.append(path.name)
                print(f'[EMPTY] {path.name}: 没有收集到任何用例')
                continue
            with redirect_stdout(buf), redirect_stderr(buf):
                result = unittest.TextTestRunner(stream=buf, verbosity=0).run(suite)
        except Exception:
            broken.append(path.name)
            print(f'[LOAD-ERROR] {path.name}')
            traceback.print_exc(limit=4)
            continue

        total += result.testsRun
        failures += len(result.failures)
        errors += len(result.errors)
        skipped += len(result.skipped)
        status = 'OK  ' if result.wasSuccessful() else 'FAIL'
        print(f'[{status}] {path.name}: ran={result.testsRun} fail={len(result.failures)} '
              f'err={len(result.errors)} skip={len(result.skipped)}')
        for case, tb in result.failures + result.errors:
            print(f'    -- {case}')
            for line in tb.strip().splitlines()[-6:]:
                print(f'       {line}')

    print('=' * 70)
    print(f'TOTAL ran={total} fail={failures} err={errors} skip={skipped} '
          f'broken={len(broken)}')
    return 0 if (failures or errors or broken) == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
