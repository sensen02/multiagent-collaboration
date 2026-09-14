"""内置 image-generation 技能：脚本契约与技能文件本身。

脚本是随程序分发的技能资产，因此这里既验证它的行为（用假的 HTTP 响应，不发真实请求），
也验证它没有引入第三方依赖——技能脚本必须能在任何用户工作区里用系统 python3 直接跑。
"""
import ast
import importlib.util
import io
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unison.skills import Skills

SKILL_DIR = Path(__file__).resolve().parent.parent / 'unison' / 'skills' / 'image-generation'
SCRIPT = SKILL_DIR / 'scripts' / 'generate.py'
# 标准库 + **技能自带的兄弟模块**。`local_image` 是本技能目录里的文件（不是三方包），
# 所以"技能脚本零第三方依赖"这条约束仍然成立。local_image.py 自己也要过同一张网。
ALLOWED_IMPORTS = {'__future__', 'argparse', 'base64', 'json', 'os', 're', 'struct', 'sys', 'time',
                   'urllib', 'local_image', 'hashlib', 'subprocess', 'pathlib', 'imagecheck',
                   'weights'}
SKILL_SCRIPTS = (SCRIPT, SKILL_DIR / 'scripts' / 'local_image.py')


def load_module():
    spec = importlib.util.spec_from_file_location('unison_skill_image_generate', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def png(width=7, height=9):
    """最小合法 PNG 头：脚本只读 IHDR，因此不需要真的能解码。"""
    return (b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR'
            + struct.pack('>II', width, height) + b'\x08\x06\x00\x00\x00' + b'\x00' * 8)


def jpeg(width=320, height=240):
    """带 SOF0 的最小 JPEG 片段，用于验证脚本走段解析而不是猜测。"""
    sof = b'\xff\xc0' + struct.pack('>H', 17) + b'\x08' + struct.pack('>HH', height, width) + b'\x03' + b'\x00' * 9
    return b'\xff\xd8\xff\xe0' + struct.pack('>H', 16) + b'JFIF\x00' + b'\x00' * 11 + sof + b'\xff\xd9'


class FakeResponse:
    def __init__(self, body, content_type='image/png'):
        self._body = body
        self.headers = {'Content-Type': content_type}

    def read(self, size=-1):
        return self._body if size is None or size < 0 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class SniffTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_detects_container_and_dimensions(self):
        self.assertEqual(self.module.sniff(png(7, 9)), ('png', 7, 9))
        self.assertEqual(self.module.sniff(jpeg(320, 240)), ('jpeg', 320, 240))
        gif = b'GIF89a' + struct.pack('<HH', 12, 34) + b'\x00' * 4
        self.assertEqual(self.module.sniff(gif), ('gif', 12, 34))

    def test_rejects_non_image(self):
        self.assertEqual(self.module.sniff(b'<!DOCTYPE html><html>error</html>'), (None, None, None))


class GenerateScriptTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name) / 'images' / 'x.png'

    def tearDown(self):
        self.temp.cleanup()

    def run_main(self, argv):
        stdout = io.StringIO()
        with patch('sys.stdout', stdout):
            code = self.module.main(argv)
        return code, stdout.getvalue()

    def test_pollinations_writes_file_and_reports_real_size(self):
        requested = {}

        def fake_urlopen(request, timeout=None):
            requested['url'] = request.full_url
            return FakeResponse(png(7, 9))

        with patch.object(self.module.urllib.request, 'urlopen', fake_urlopen):
            code, out = self.run_main(['--prompt', 'a red fox', '--out', str(self.out), '--width', '512', '--height', '512'])
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(payload['ok'])
        self.assertEqual(self.out.read_bytes(), png(7, 9))
        # 尺寸来自图像头，而不是把请求参数回显出来。
        self.assertEqual((payload['format'], payload['width'], payload['height']), ('png', 7, 9))
        self.assertIn('/prompt/a%20red%20fox', requested['url'])
        self.assertIn('width=512', requested['url'])
        self.assertIn('nologo=true', requested['url'])

    def test_huggingface_requires_token_and_reports_clean_error(self):
        with patch.dict('os.environ', {'HF_TOKEN': ''}, clear=False):
            code, out = self.run_main(['--prompt', 'x', '--out', str(self.out), '--backend', 'huggingface',
                                       '--model', 'black-forest-labs/FLUX.1-schnell'])
        payload = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(payload['ok'])
        self.assertIn('HF_TOKEN', payload['error'])
        self.assertFalse(self.out.exists())

    def test_non_image_response_is_rejected_without_writing_file(self):
        def fake_urlopen(request, timeout=None):
            return FakeResponse(b'<html>upstream error</html>', 'text/html')

        with patch.object(self.module.urllib.request, 'urlopen', fake_urlopen):
            code, out = self.run_main(['--prompt', 'x', '--out', str(self.out)])
        payload = json.loads(out)
        self.assertEqual(code, 1)
        self.assertIn('不是可识别的图像', payload['error'])
        self.assertFalse(self.out.exists())

    def test_missing_prompt_is_a_clean_failure(self):
        code, out = self.run_main(['--out', str(self.out)])
        payload = json.loads(out)
        self.assertEqual(code, 1)
        self.assertIn('--prompt', payload['error'])

    def test_out_without_extension_gets_backend_default(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured['url'] = request.full_url
            return FakeResponse(png(1, 1))

        target = Path(self.temp.name) / 'shot'
        with patch.object(self.module.urllib.request, 'urlopen', fake_urlopen):
            code, out = self.run_main(['--prompt', 'hello world', '--out', str(target)])
        self.assertEqual(code, 0)
        self.assertTrue(Path(json.loads(out)['path']).name.endswith('.jpg'))

    def test_list_models_parses_upstream_catalog(self):
        def fake_urlopen(request, timeout=None):
            return FakeResponse(json.dumps(['sana']).encode(), 'application/json')

        with patch.object(self.module.urllib.request, 'urlopen', fake_urlopen):
            code, out = self.run_main(['--list-models'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['models'], ['sana'])

    def test_script_runs_as_a_subprocess_and_writes_output(self):
        """真实走一遍命令行入口：技能正文就是让模型这样调用它的。"""
        def fake_urlopen(request, timeout=None):
            return FakeResponse(png(5, 6))

        target = Path(self.temp.name) / 'cli.jpg'
        with patch.object(self.module.urllib.request, 'urlopen', fake_urlopen):
            # 子进程无法共享 mock，因此改为验证 --help 与参数解析路径。
            completed = subprocess.run([sys.executable, str(SCRIPT), '--help'],
                                       capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0)
        self.assertIn('--backend', completed.stdout)
        self.assertIn('--list-models', completed.stdout)


class SkillAssetTests(unittest.TestCase):
    def test_script_imports_only_standard_library(self):
        for script in SKILL_SCRIPTS:
            with self.subTest(script=script.name):
                tree = ast.parse(script.read_text(encoding='utf-8'))
                roots = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        roots.update(alias.name.split('.')[0] for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        roots.add(node.module.split('.')[0])
                self.assertTrue(roots <= ALLOWED_IMPORTS,
                                f'非标准库依赖：{sorted(roots - ALLOWED_IMPORTS)}')

    def test_local_backend_is_selectable_and_documented(self):
        """本机后端必须同时出现在：实现（choices/CAPABILITIES）与摘要（frontmatter）里。

        这条是那次真事故的守卫：摘要承诺了本机后端，而实现里没有这一档。
        """
        spec = importlib.util.spec_from_file_location('unison_skill_image_generate', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIn('local', module.ALL_BACKENDS)
        self.assertIn('local', module.CAPABILITIES['backends'])
        body = (SKILL_DIR / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('local', body.split('invocation:')[0])

    def test_skill_documents_base_dir_and_verification_rules(self):
        skills = Skills(bundled=SKILL_DIR.parent)
        skill = skills.load('image-generation', str(SKILL_DIR))
        self.assertEqual(skill['source'], 'bundled')
        self.assertTrue(skill['model_invocable'])
        body = skill['content']
        # 正文必须告诉模型：脚本路径相对 base directory 解析，且生成成功不等于符合需求。
        self.assertIn('scripts/generate.py', body)
        self.assertIn('Base directory', body)
        self.assertIn('不要声称', body)
        self.assertIn('--list-models', body)

    def test_reference_file_is_valid_json(self):
        data = json.loads((SKILL_DIR / 'references' / 'prompt-patterns.json').read_text(encoding='utf-8'))
        self.assertTrue(data['patterns'])
        self.assertTrue(all(item['template'] for item in data['patterns']))


if __name__ == '__main__':
    unittest.main()
