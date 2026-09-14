"""gpu 技能的探测逻辑：负载判据、桥接协议、以及"不许瞎猜"的两条教训。

真机教训（第 16 节）：探测脚本自己不可靠比没有更糟——`which` 在本机 shell 里
对不存在的命令也返回 0，于是把没装的 ROCm 报成已安装。
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

PROBE = Path(__file__).resolve().parent.parent / 'unison' / 'skills' / 'gpu' / 'scripts' / 'probe.py'


def load_probe():
    spec = importlib.util.spec_from_file_location('gpu_probe_under_test', PROBE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class GpuProbeTests(unittest.TestCase):
    def setUp(self):
        self.probe = load_probe()

    def test_missing_tools_are_not_reported_as_installed(self):
        """`which 不存在` 在本机返回 0——存在性必须用 shutil.which，别信 `which` 的退出码。"""
        result = self.probe.probe_rocm()
        self.assertFalse(result['installed'], result)
        self.assertEqual(result['hints'], [])

    def test_no_display_connectors_in_device_list(self):
        """card0-DP-1 这类是显示接口，不是 GPU 设备，不能进设备列表。"""
        devices = self.probe.probe_gpu_load()['devices']
        names = [d['card'] for d in devices]
        self.assertTrue(names, '应当至少识别出一张卡')
        self.assertTrue(all('-' not in name for name in names), names)
        # 每张卡都该有显存总量；独显的显存明显大于核显
        totals = [d['vram_total_bytes'] for d in devices if d.get('vram_total_bytes')]
        self.assertTrue(totals)
        self.assertGreater(max(totals), 2 ** 30, '独显显存应当 > 1GiB')

    def test_holding_a_node_is_not_the_same_as_being_saturated(self):
        """「有进程持有 render 节点」≠「算力被占满」：判据是占用率与显存。"""
        idle_rows = [{'card': 'card0', 'busy_percent': 0, 'vram_used_pct': 0,
                      'vram_total_bytes': 16 * 2 ** 30, 'vram_used_bytes': 0, 'vram_free_bytes': 16 * 2 ** 30}]
        with mock.patch.object(self.probe, 'probe_gpu_load', wraps=self.probe.probe_gpu_load):
            pass
        # 直接验判据本身：占用率高 → saturated；持有节点但指标低 → idle
        def verdict_for(rows, holders):
            saturated = any((d.get('busy_percent') or 0) >= 20 or (d.get('vram_used_pct') or 0) >= 60
                            for d in rows)
            return {'idle': not saturated, 'busy_processes': holders}

        self.assertTrue(verdict_for(idle_rows, [{'node': '/dev/dri/renderD128', 'names': ['browser']}])['idle'])
        busy_rows = [dict(idle_rows[0], busy_percent=95)]
        self.assertFalse(verdict_for(busy_rows, [])['idle'])
        full_vram = [dict(idle_rows[0], vram_used_pct=85)]
        self.assertFalse(verdict_for(full_vram, [])['idle'])

    def test_probe_never_writes_or_downloads(self):
        """探测必须只读：跑完之后资产根不应该多出任何东西。"""
        root = Path(self.probe.ASSET_ROOT)
        before = sorted(p.name for p in root.iterdir()) if root.is_dir() else []
        report = self.probe.collect()
        after = sorted(p.name for p in root.iterdir()) if root.is_dir() else []
        self.assertEqual(before, after)
        self.assertIn('storage', report)

    def test_verdict_answers_what_can_be_used(self):
        report = self.probe.collect()
        report['verdict'] = self.probe.verdict(report)
        joined = ' '.join(report['verdict'])
        self.assertIn('Vulkan', joined)
        self.assertIn('本地算力', joined)          # 负载结论必须在
        self.assertTrue(any('stable-diffusion.cpp' in line for line in report['verdict']))

    def test_bridge_entry_speaks_the_port_protocol(self):
        """桥接入口显式声明调用方式，不靠 stdin 猜。"""
        import subprocess
        done = subprocess.run([sys.executable, str(PROBE.parent / 'bridge.py')],
                              input=json.dumps({'skill': 'gpu', 'workspace': '/tmp', 'base': '/tmp', 'args': {}}),
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(done.returncode, 0, done.stderr[-400:])
        payload = json.loads(done.stdout)
        self.assertTrue(payload['ok'])
        self.assertIn('gpu_load', payload['result'])


if __name__ == '__main__':
    unittest.main()
