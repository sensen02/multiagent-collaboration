"""技能（skill）机制：frontmatter、多根竞争、目录渲染、运行时加载与 HTTP 契约。

这些是隔离临时目录中的机制测试：不调用任何真实模型，也不依赖机器上已有的技能。
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from unison.runtime import Runtime
from unison.skills import (Skills, SkillError, discover, is_skill_name, parse_frontmatter,
                           project_root_of, render_catalog, render_content, roots_for)
from unison.tools import TOOLS


def write_skill(root, name, body='# body\n', **frontmatter):
    """写一个目录包技能，返回其 SKILL.md 路径。"""
    directory = Path(root) / name
    directory.mkdir(parents=True, exist_ok=True)
    fields = {'name': name, 'description': f'{name} 的描述'}
    fields.update(frontmatter)
    lines = ['---']
    for key, value in fields.items():
        if value is None:
            continue
        key = key.replace('_', '-')
        lines.append(f'{key}: {value}')
    lines.append('---')
    path = directory / 'SKILL.md'
    path.write_text('\n'.join(lines) + '\n' + body, encoding='utf-8')
    return path


class FrontmatterTests(unittest.TestCase):
    def test_plain_scalars_and_name_grammar(self):
        fields, body = parse_frontmatter('---\nname: image-gen\ndescription: 生成图像\nwhen-to-use: 需要配图时\n---\n正文\n')
        self.assertEqual(fields['name'], 'image-gen')
        self.assertEqual(fields['description'], '生成图像')
        self.assertEqual(fields['when-to-use'], '需要配图时')
        self.assertEqual(body, '正文\n')
        self.assertTrue(is_skill_name('a-b-c2'))
        for bad in ('Image-Gen', 'image_gen', '-lead', 'trail-', '', 'x' * 200):
            self.assertFalse(is_skill_name(bad), bad)

    def test_nested_metadata_and_inline_list(self):
        fields, _ = parse_frontmatter('---\nname: a\ndescription: d\nmetadata:\n  note: hello\n  tags: [x, y]\n---\n')
        self.assertEqual(fields['metadata'], {'note': 'hello', 'tags': ['x', 'y']})

    def test_block_scalar_and_folded_continuation(self):
        block, _ = parse_frontmatter('---\nname: a\ndescription: |\n  第一行\n  第二行\n---\n')
        self.assertEqual(block['description'], '第一行\n第二行')
        folded, _ = parse_frontmatter('---\nname: a\ndescription: 折行\n  续写\n---\n')
        self.assertEqual(folded['description'], '折行 续写')

    def test_missing_frontmatter_is_rejected(self):
        self.assertEqual(parse_frontmatter('没有 frontmatter')[0], {})


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.home = self.base / 'home'
        (self.home / '.dsh' / 'skills').mkdir(parents=True)
        self.bundled = self.base / 'bundled'
        self.bundled.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def skills(self, workspace=None):
        return Skills(home=self.home, bundled=self.bundled)

    def test_roots_include_workspace_user_and_bundled_in_rank_order(self):
        roots = roots_for(workspace=self.project, home=self.home, bundled=self.bundled)
        ranks = [item['rank'] for item in roots]
        self.assertEqual(ranks, sorted(ranks))
        sources = [item['source'] for item in roots]
        self.assertIn('workspace-dsh', sources)
        self.assertIn('user-dsh', sources)
        self.assertIn('bundled', sources)
        # 工作区自己的技能根优先于用户根与内置包。
        self.assertLess(sources.index('workspace-dsh'), sources.index('user-dsh'))
        self.assertLess(sources.index('user-dsh'), sources.index('bundled'))

    def test_explicit_project_root_is_scanned_after_user_roots(self):
        """子工作区副本在别处时，项目根技能仍可被发现，但优先于内置包、低于用户根。"""
        copy = self.base / 'data' / 'workspaces' / 'task_x'
        copy.mkdir(parents=True)
        roots = roots_for(workspace=copy, home=self.home, bundled=self.bundled, project_root=self.project)
        order = {item['source']: item['rank'] for item in roots}
        self.assertLess(order['workspace-dsh'], order['user-dsh'])
        self.assertLess(order['user-dsh'], order['project-root'])
        self.assertLess(order['project-root'], order['bundled'])
        self.assertTrue(any(str(item['root']).startswith(str(self.project)) for item in roots
                            if item['source'] == 'project-root'))

    def test_project_root_falls_back_to_git_ancestor(self):
        nested = self.project / 'src' / 'pkg'
        nested.mkdir(parents=True)
        self.assertIsNone(project_root_of(nested))
        (self.project / '.git').mkdir()
        self.assertEqual(project_root_of(nested), self.project.resolve())

    def test_project_skill_wins_over_bundled_and_is_reported(self):
        write_skill(self.bundled, 'shared', body='内置版本\n')
        write_skill(self.project / '.dsh' / 'skills', 'shared', body='项目版本\n')
        winners, diagnostics, _ = discover(workspace=self.project, home=self.home, bundled=self.bundled)
        winner = next(item for item in winners if item['name'] == 'shared')
        self.assertEqual(winner['source'], 'workspace-dsh')
        self.assertIn('项目版本', winner['content'])
        self.assertTrue(any('覆盖' in item['error'] for item in diagnostics))

    def test_flat_markdown_skill_and_name_must_match_file(self):
        root = self.project / '.agents' / 'skills'
        root.mkdir(parents=True)
        (root / 'flat-one.md').write_text('---\nname: flat-one\ndescription: 扁平技能\n---\n正文\n', encoding='utf-8')
        winners, diagnostics, _ = discover(workspace=self.project, home=self.home)
        self.assertEqual([item['name'] for item in winners], ['flat-one'])
        self.assertEqual(diagnostics, [])
        # 名称与文件名不一致时整条拒绝，并给出可读原因。
        (root / 'flat-two.md').write_text('---\nname: other-name\ndescription: d\n---\n正文\n', encoding='utf-8')
        winners, diagnostics, _ = discover(workspace=self.project, home=self.home)
        self.assertEqual([item['name'] for item in winners], ['flat-one'])
        self.assertTrue(any('必须与文件名' in item['error'] for item in diagnostics))

    def test_bad_skill_is_rejected_without_hiding_good_ones(self):
        root = self.project / '.dsh' / 'skills'
        write_skill(root, 'good-one')
        (root / 'missing-description').mkdir(parents=True)
        (root / 'missing-description' / 'SKILL.md').write_text('---\nname: missing-description\n---\n正文\n', encoding='utf-8')
        (root / 'bad-bool').mkdir(parents=True)
        (root / 'bad-bool' / 'SKILL.md').write_text(
            '---\nname: bad-bool\ndescription: d\ndisable-model-invocation: maybe\n---\n正文\n', encoding='utf-8')
        winners, diagnostics, _ = discover(workspace=self.project, home=self.home)
        self.assertEqual([item['name'] for item in winners], ['good-one'])
        errors = ' '.join(item['error'] for item in diagnostics)
        self.assertIn('description', errors)
        self.assertIn('必须是布尔值', errors)

    def test_disable_model_invocation_keeps_skill_out_of_catalog(self):
        write_skill(self.bundled, 'hidden-one', disable_model_invocation='true')
        write_skill(self.bundled, 'visible-one')
        skills = self.skills()
        catalog = [item['name'] for item in skills.catalog(str(self.project))]
        self.assertEqual(catalog, ['visible-one'])
        with self.assertRaises(SkillError):
            skills.load('hidden-one', str(self.project))
        # 人可以在目录里看到它，模型不能加载。
        self.assertIn('hidden-one', [item['name'] for item in skills.snapshot(str(self.project))['skills']])

    def test_new_skill_directory_is_picked_up_without_restart(self):
        skills = self.skills()
        self.assertEqual(skills.catalog(str(self.project)), [])
        write_skill(self.bundled, 'late-arrival')
        self.assertEqual([item['name'] for item in skills.catalog(str(self.project))], ['late-arrival'])

    def test_catalog_and_content_rendering(self):
        write_skill(self.bundled, 'demo-one', body='步骤一\n', when_to_use='当任务需要演示时')
        skills = self.skills()
        catalog = render_catalog(skills.catalog(str(self.project)), base_dirs=True)
        self.assertIn('<available_skills>', catalog)
        self.assertIn('- `demo-one`: demo-one 的描述', catalog)
        loaded = skills.load('demo-one', str(self.project))
        content = render_content(loaded)
        self.assertTrue(content.startswith('<skill_content name="demo-one">'))
        self.assertIn('Base directory for this skill:', content)
        self.assertIn('<skill_instructions>', content)
        self.assertIn('步骤一', content)

    def test_unknown_and_invalid_names_report_available_skills(self):
        write_skill(self.bundled, 'only-one')
        skills = self.skills()
        with self.assertRaisesRegex(SkillError, 'only-one'):
            skills.load('nope', str(self.project))
        with self.assertRaisesRegex(SkillError, 'kebab-case'):
            skills.load('Not A Name', str(self.project))


class RuntimeSkillTests(unittest.IsolatedAsyncioTestCase):
    """运行时接入：目录注入历史、skill 工具加载、事件记录。"""

    async def asyncSetUp(self):
        from unison.demo import demo_call
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        (self.project / 'README.md').write_text('original\n')
        self.r = Runtime(Path(self.temp.name) / 'data', concurrency=1, batch_seconds=.01)
        self.r.models.adapters['fake'] = demo_call
        self.r.store.put('model', {'id': 'test', 'model': 'test', 'adapter': 'fake', 'base_url': 'test://local',
                                   'no_key': True, 'context_window': 64000, 'max_output': 4096})
        # 把内置技能替换成临时目录里的技能，测试不依赖随程序分发的具体技能。
        self.bundled = Path(self.temp.name) / 'bundled'
        write_skill(self.bundled, 'tmp-skill', body='临时技能正文\n')
        self.r.skills = Skills(home=Path(self.temp.name) / 'home', bundled=self.bundled)

    async def asyncTearDown(self):
        await self.r.stop()
        self.temp.cleanup()

    def create(self):
        run = self.r.create_run('技能测试', str(self.project), 'test')
        return run, self.r.task(run['root_task'])

    async def test_task_history_gets_skill_catalog_system_message(self):
        run, task = self.create()
        history = self.r.task_history(task)
        self.assertEqual([m['role'] for m in history[:3]], ['system', 'system', 'user'])
        self.assertIn('<available_skills>', history[1]['content'])
        self.assertIn('tmp-skill', history[1]['content'])
        # 系统提示词是多条 system 消息的拼接，可逐字重建用于比对。
        prompt = self.r.summarize_prompt(history)
        self.assertIn('Unison 本地协作系统', prompt)
        self.assertIn('<available_skills>', prompt)
        header = self.r.record_request(task, history)
        assembled = self.r.assemble_request(header)
        import hashlib
        self.assertEqual(header['prompt_sha256'], hashlib.sha256(assembled['prompt'].encode()).hexdigest())

    async def test_skill_tool_returns_framed_content_and_records_event(self):
        run, task = self.create()
        result = await self.r.tool_skill(self.r.task(task['id']), {'name': 'tmp-skill'})
        self.assertEqual(result['name'], 'tmp-skill')
        self.assertTrue(result['content'].startswith('<skill_content name="tmp-skill">'))
        self.assertIn('临时技能正文', result['content'])
        self.assertEqual(result['source'], 'bundled')
        events = [e for e in self.r.store.events() if e['type'] == 'SkillLoaded']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['payload']['name'], 'tmp-skill')

    async def test_skill_tool_error_lists_available_skills(self):
        run, task = self.create()
        result = await self.r.tool_skill(self.r.task(task['id']), {'name': 'missing-skill'})
        self.assertIn('error', result)
        self.assertEqual([item['name'] for item in result['available_skills']], ['tmp-skill'])

    async def test_child_workspace_sees_the_project_skill_root(self):
        """子任务工作副本在数据目录里，项目根必须来自任务记录，而不是路径推断。

        这是之前的实现缺陷：按物理祖先向上找技能根，而副本的祖先跟真实项目无关，
        于是任务副本完全看不到项目自己的技能目录。
        """
        run, task = self.create()
        child = self.r.create_task(run, '子任务', 'test', task)
        write_skill(self.project / '.dsh' / 'skills', 'project-only')
        self.assertEqual(self.r.task_project_root(child), str(self.project.resolve()))
        names = [item['name'] for item in self.r.skills.catalog(child['workspace'], project_root=self.r.task_project_root(child))]
        self.assertIn('tmp-skill', names)
        self.assertIn('project-only', names)
        # 同名时工作区自己的技能根优先于项目根。
        write_skill(Path(child['workspace']) / '.dsh' / 'skills', 'project-only', body='副本版本\n')
        winner = next(item for item in self.r.skills.catalog(child['workspace'], project_root=self.r.task_project_root(child))
                      if item['name'] == 'project-only')
        self.assertEqual(winner['source'], 'workspace-dsh')
        self.assertIn('副本版本', winner['content'])


class SkillToolSchemaTests(unittest.TestCase):
    def test_skill_tool_schema_declares_name_and_batch(self):
        schema = next(item for item in TOOLS if item['function']['name'] == 'skill')
        parameters = schema['function']['parameters']
        # name 不再强制：允许只给 batch 的调用形式由运行时按技能契约校验并给出可读错误。
        self.assertIn('name', parameters['properties'])
        self.assertIn('batch', parameters['properties'])
        self.assertEqual(parameters['properties']['batch']['type'], 'array')

    def test_skills_list_tool_is_registered(self):
        schema = next(item for item in TOOLS if item['function']['name'] == 'skills_list')
        self.assertEqual(schema['function']['parameters']['properties'], {})


if __name__ == '__main__':
    unittest.main()
