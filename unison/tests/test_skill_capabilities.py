# -*- coding: utf-8 -*-
"""本地后端（image-generation / gpu 技能）的回归测试：用假 sd-cli，不依赖真显卡。

清理后的分工很明确：脚本**只测量与记录**，不下判词。

- `weights.py` 读 safetensors 文件头，报结构事实（tensor 前缀直方图、缺哪些部件），
  不再输出 `role` / `verdict` 这种角色判词。
- `imagecheck.py` 量画面统计量（主色占比、对比度、块标准差…），不再输出 `degenerate` / `verdict`。
- `local_image.py` 跑完之后把统计量写进结果与溯源，**不因为统计量难看就拒绝交付**。

因此这里断言的是"数字与事实对不对"，而不是"运行时判没判它废图"。
原先"技能声明 vs 执行体自报"的对账（`unison/skillcheck.py` + `skill-validator` 技能）
与工作区端口/证据对账已整体移除：那是运行时自建的一套能力官僚机制，
用关键词匹配和端口正则替模型判断技能可不可信。
"""
from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / 'unison' / 'skills'
IMAGE_SCRIPTS = SKILLS_ROOT / 'image-generation' / 'scripts'
GPU_SCRIPTS = SKILLS_ROOT / 'gpu' / 'scripts'
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(IMAGE_SCRIPTS))
sys.path.insert(0, str(GPU_SCRIPTS))

import imagecheck  # noqa: E402
import local_image  # noqa: E402
import weights  # noqa: E402


def write(path: Path, text: str, executable: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class LocalBackendTests(unittest.TestCase):
    """本地后端：用假 sd-cli 跑，不碰真显卡，也不联网。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.local_image = local_image

    def make_sd_cli(self, body: str, name='sd-cli'):
        script = self.dir / name
        write(script, '#!/usr/bin/env python3\n' + body, executable=True)
        return script

    def make_png(self, path: Path, width=64, height=64, stripes=False):
        """写一张真 PNG（用于退化检查）：stripes=True 时列间强交替。

        非 stripes 模式要**颜色丰富**，否则会被退化检查判成纯色废图——
        测试替身必须造出"正常图"，不然测的是检查器而不是被测逻辑。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        import struct
        import zlib
        state = 0x2545F491
        rows = []
        for y in range(height):
            row = bytearray()
            for x in range(width):
                if stripes:
                    value = 255 if x % 2 == 0 else 0
                else:
                    state = (state * 1103515245 + 12345) & 0x7FFFFFFF
                    value = (state >> 7) % 256
                row += bytes((value, (value * 3 + x) % 256, (value * 7 + y) % 256))
            rows.append(b'\x00' + bytes(row))
        raw = zlib.compress(b''.join(rows))
        def chunk(kind, data):
            return (struct.pack('>I', len(data)) + kind + data
                    + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))
        png = (b'\x89PNG\r\n\x1a\n'
               + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
               + chunk(b'IDAT', raw) + chunk(b'IEND', b''))
        path.write_bytes(png)
        return path

    def fake_checkpoint(self, name='GuoFeng3.4.safetensors'):
        """造一个"看起来完整"的 checkpoint 文件头（只写头，不写权重）。"""
        import struct
        header = {}
        for index in range(3):
            header[f'model.diffusion_model.weight{index}'] = {'dtype': 'F16', 'shape': [1],
                                                              'data_offsets': [0, 2]}
            header[f'first_stage_model.weight{index}'] = {'dtype': 'F16', 'shape': [1],
                                                          'data_offsets': [0, 2]}
            header[f'cond_stage_model.weight{index}'] = {'dtype': 'F16', 'shape': [1],
                                                         'data_offsets': [0, 2]}
        blob = json.dumps(header).encode()
        path = self.dir / name
        path.write_bytes(struct.pack('<Q', len(blob)) + blob + b'\x00' * 32)
        return path

    def fake_lora(self, name='pixel.safetensors'):
        import struct
        header = {f'lora_unet_block_{i}.weight': {'dtype': 'F16', 'shape': [1],
                                                  'data_offsets': [0, 2]} for i in range(6)}
        blob = json.dumps(header).encode()
        path = self.dir / name
        path.write_bytes(struct.pack('<Q', len(blob)) + blob + b'\x00' * 16)
        return path

    def test_weight_structure_is_reported_without_role_verdict(self):
        """只报文件头里的结构事实，不再换算成"角色"或"能不能用"。"""
        checkpoint = weights.classify(self.fake_checkpoint())
        groups = checkpoint['tensor_groups']
        self.assertTrue(groups['unet'] and groups['vae'] and groups['clip'])
        self.assertEqual(checkpoint['architecture'], 'sd15')
        # 判词字段已移除
        for gone in ('role', 'verdict', 'usable_as', 'not_usable_as'):
            self.assertNotIn(gone, checkpoint, gone)
        lora = weights.classify(self.fake_lora())
        self.assertGreater(lora['lora_tensor_fraction'], 0.5)
        self.assertNotIn('role', lora)
        self.assertTrue(lora.get('observations'))

    def test_refuses_lora_as_main_model_without_generating(self):
        """角色不对时必须拒绝执行：不能先生成一批再让人从废图里发现。"""
        lora = self.fake_lora()
        called = self.dir / 'called.txt'
        cli = self.make_sd_cli(f"open({str(called)!r},'w').write('ran')\n")
        with self.assertRaises(RuntimeError) as ctx:
            self.local_image.generate_local(
                [{'prompt': 'x', 'model': str(lora), 'sd_cli': str(cli), 'out': str(self.dir / 'o.png')}],
                workspace=str(self.dir))
        self.assertIn('checkpoint', str(ctx.exception))
        self.assertFalse(called.exists(), '角色不对却已经调用了 sd-cli')

    def test_lora_not_applied_yields_warning(self):
        """命令成功但日志里没有 apply_loras → 必须给 warning（静默忽略是真实失败模式）。"""
        model = self.fake_checkpoint()
        out = self.dir / 'out.png'
        png = str(out)
        cli = self.make_sd_cli(
            'import sys, shutil\n'
            f"shutil.copyfile({str(self.make_png(self.dir / 'src.png'))!r}, "
            f"sys.argv[sys.argv.index('-o') + 1])\n"
            "print('[INFO] done, no lora line')\n")
        envelope = self.local_image.generate_local(
            [{'prompt': 'x', 'model': str(model), 'sd_cli': str(cli), 'lora': 'pixel',
              'lora_dir': str(self.dir), 'out': png, 'width': 64, 'height': 64}],
            workspace=str(self.dir))
        item = envelope['items'][0]
        self.assertFalse(item['lora_applied'])
        self.assertIn('LoRA', item.get('warning', ''))

    def test_low_structure_output_is_delivered_with_measurements(self):
        """画面结构很少时**仍然交付**，只把统计量如实带上，由模型判断能不能用。

        原先这里断言的是"必须被判为失败并抛 LocalGenerationError"。那正是
        "运行时替模型下判断"的形状：命令成功了、图也写出来了，却因为几个阈值
        拒绝交付已经存在的产物。现在图照常交付，数字照常记录。
        """
        model = self.fake_checkpoint()
        stripes = self.make_png(self.dir / 'stripes.png', width=128, height=128, stripes=True)
        cli = self.make_sd_cli(
            'import sys, shutil\n'
            f"shutil.copyfile({str(stripes)!r}, sys.argv[sys.argv.index('-o') + 1])\n")
        out = self.dir / 'bad.png'
        envelope = self.local_image.generate_local(
            [{'prompt': 'x', 'model': str(model), 'sd_cli': str(cli), 'out': str(out),
              'width': 128, 'height': 128}], workspace=str(self.dir))
        item = envelope['items'][0]
        self.assertTrue(item['ok'])
        self.assertTrue(out.is_file())
        check = item['check']
        # 测量值在：主色占比、色数、对比度、块标准差、重复列
        for key in ('dominant_fraction', 'distinct_colors', 'local_contrast',
                    'block_std', 'repeated_columns'):
            self.assertIn(key, check, key)
        # 判词不在
        for gone in ('degenerate', 'verdict'):
            self.assertNotIn(gone, check, gone)

    def test_imagecheck_reports_numbers_and_no_verdict(self):
        """imagecheck 只测量：没有 degenerate / verdict 字段，但每个统计量都在。"""
        flat = self.make_png(self.dir / 'flat.png', width=64, height=64)
        report = imagecheck.analyse(flat)
        for gone in ('degenerate', 'verdict', 'flat_by_design', 'block_std_floor'):
            self.assertNotIn(gone, report, gone)
        for key in ('dominant_color', 'dominant_fraction', 'distinct_colors',
                    'local_contrast', 'block_std', 'repeated_columns'):
            self.assertIn(key, report, key)

    def test_provenance_records_command_and_hashes(self):
        """溯源必须足以重建这次调用：完整命令行 + 权重 sha256 + 输出 sha256。"""
        model = self.fake_checkpoint()
        src = self.make_png(self.dir / 'good-src.png', width=64, height=64)
        cli = self.make_sd_cli(
            'import sys, shutil\n'
            f"shutil.copyfile({str(src)!r}, sys.argv[sys.argv.index('-o') + 1])\n")
        out = self.dir / 'out' / 'a.png'
        envelope = self.local_image.generate_local(
            [{'prompt': 'a sword', 'model': str(model), 'sd_cli': str(cli), 'out': str(out),
              'seed': 7, 'width': 64, 'height': 64, 'steps': 3}], workspace=str(self.dir))
        provenance = json.loads(Path(envelope['provenance']).read_text(encoding='utf-8'))
        item = provenance['items'][0]
        self.assertEqual(item['seed'], 7)
        self.assertIn('--steps', item['command'])
        self.assertEqual(item['sha256'], envelope['items'][0]['sha256'])
        self.assertEqual(provenance['model_sha256'], self.local_image.sha256_file(model))
        self.assertEqual(provenance['backend'], 'local')
        # 二进制版本：没有它，"可复现"就少一半（换一次构建可能就不是同一张图）
        self.assertIn('sd_cli_version', provenance)
        self.assertIn('classify', dir(weights))                      # 文件头事实与溯源同源
        self.assertIn('--backend', item['command'])

    def test_backend_list_matches_frontmatter_and_implementation(self):
        """SKILL.md 声明、generate.py 的 choices、CAPABILITIES 三者必须一致。"""
        import generate
        import re
        text = (SKILLS_ROOT / 'image-generation' / 'SKILL.md').read_text(encoding='utf-8')
        block = re.search(r'metadata:(.*?)\ninvocation:', text, re.S).group(1)
        declared = re.findall(r'^\s*-\s*([a-z0-9-]+)\s*$', block, re.M)
        self.assertEqual(sorted(declared), sorted(generate.CAPABILITIES['backends']))
        self.assertEqual(set(generate.ALL_BACKENDS), set(generate.CAPABILITIES['backends']))
        self.assertEqual(len(generate.ALL_BACKENDS), len(set(generate.ALL_BACKENDS)))




if __name__ == '__main__':
    unittest.main()
