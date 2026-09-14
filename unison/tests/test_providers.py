"""Provider 目录（DSH 风格 provider + models）的统一结构测试。"""

import tempfile
import unittest
from pathlib import Path

from unison import providers
from unison.models import Models
from unison.store import Store


class ProviderShapeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'data')
        self.models = Models(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_dsh_api_maps_to_single_runtime_adapter(self):
        self.assertEqual(providers.API_ADAPTERS['openai-completions'], 'chat-completions')
        self.assertEqual(providers.API_ADAPTERS['openai-responses'], 'responses')
        self.assertEqual(providers.api_of({'api': 'openai-responses'}), 'openai-responses')
        self.assertEqual(providers.api_of({'adapter': 'chat-completions'}), 'openai-completions')
        self.assertEqual(providers.api_of({'wire_api': 'responses'}), 'openai-responses')
        self.assertEqual(providers.adapter_of({'api': 'openai-responses'}), 'responses')
        # 未登记的 adapter 不冒充任何 wire api
        self.assertEqual(providers.api_of({'adapter': 'demo'}), '')
        self.assertEqual(providers.adapter_of({'adapter': 'demo'}), 'demo')

    def test_provider_record_normalizes_dsh_fields_and_clamps_limits(self):
        record = providers.provider_record({
            'id': 'acme', 'displayName': 'Acme Gateway', 'api': 'openai-responses',
            'baseURL': 'https://gateway.example/v1/', 'apiKeyEnv': 'ACME_KEY',
            'defaultContextWindow': 100000, 'defaultMaxTokens': 12000,
            'headers': {'X-Tenant': 'fixture'},
            'models': [{'id': 'main', 'name': 'Main'}, {'id': 'tiny', 'contextWindow': 4000, 'maxTokens': 9000}],
        })
        self.assertEqual(record['api'], 'openai-responses')
        self.assertEqual(record['adapter'], 'responses')
        self.assertEqual(record['base_url'], 'https://gateway.example/v1')
        self.assertEqual(record['default_model'], 'main')
        self.assertEqual(record['models'][0]['contextWindow'], 100000)
        self.assertEqual(record['models'][0]['maxTokens'], 12000)
        # 输出上限必须小于上下文窗口
        self.assertLess(record['models'][1]['maxTokens'], record['models'][1]['contextWindow'])

    def test_provider_record_rejects_duplicates_and_bad_headers(self):
        with self.assertRaisesRegex(ValueError, '重复'):
            providers.provider_record({'id': 'x', 'base_url': 'https://x.test', 'models': [{'id': 'm'}, {'id': 'm'}]})
        with self.assertRaisesRegex(ValueError, 'headers'):
            providers.provider_record({'id': 'x', 'base_url': 'https://x.test', 'headers': {'A': 1}, 'models': [{'id': 'm'}]})
        with self.assertRaisesRegex(ValueError, 'base_url'):
            providers.provider_record({'id': 'x', 'models': [{'id': 'm'}]})

    def test_save_provider_projects_flat_model_records(self):
        saved = providers.save_provider(self.store, self.models, {
            'id': 'acme', 'name': 'Acme', 'api': 'openai-responses', 'base_url': 'https://gateway.example/v1',
            'key_env': 'ACME_KEY', 'api_key': 'secret', 'models': [{'id': 'main', 'name': 'Main'}, {'id': 'review'}],
        })
        self.assertEqual(saved['default_model'], 'main')
        record = self.store.get('model', 'acme:main')
        self.assertEqual(record['model'], 'main')
        self.assertEqual(record['api'], 'openai-responses')
        self.assertEqual(record['adapter'], 'responses')
        self.assertTrue(record['default_model'])
        self.assertFalse(self.store.get('model', 'acme:review')['default_model'])
        # model 列表向控制台隐藏密钥
        listed = {m['id']: m for m in self.models.list()}
        self.assertNotIn('api_key', listed['acme:main'])
        self.assertEqual(listed['acme:main']['provider_name'], 'Acme')

    def test_directory_exposes_providers_and_unmanaged_models(self):
        self.models.adapters['demo'] = lambda *args: None  # server 在启动时注册
        providers.save_provider(self.store, self.models, {
            'id': 'acme', 'name': 'Acme', 'api': 'openai-completions', 'base_url': 'https://gateway.example/v1',
            'no_key': True, 'models': [{'id': 'main'}],
        })
        self.models.save({'id': 'offline-demo', 'label': '离线机制演示', 'model': 'scripted-demo',
                          'base_url': 'demo://local', 'adapter': 'demo', 'no_key': True,
                          'context_window': 64000, 'max_output': 4096})
        directory = self.models.providers()
        self.assertEqual([p['id'] for p in directory], ['acme'])
        self.assertEqual(directory[0]['models'][0]['id'], 'main')
        self.assertTrue(directory[0]['models'][0]['default'])
        self.assertEqual([m['id'] for m in providers.unmanaged_models(self.store, self.models)], ['offline-demo'])

    def test_saved_provider_changes_are_visible_to_model_list(self):
        providers.save_provider(self.store, self.models, {
            'id': 'acme', 'name': 'Acme', 'api': 'openai-responses', 'base_url': 'https://first.example',
            'models': [{'id': 'main'}],
        })
        providers.save_provider(self.store, self.models, {
            'id': 'acme', 'name': 'Acme', 'api': 'openai-responses', 'base_url': 'https://second.example',
            'models': [{'id': 'main'}],
        })
        record = self.store.get('model', 'acme:main')
        self.assertEqual(record['base_url'], 'https://second.example')
        self.assertEqual(len(self.store.all('provider')), 1)


if __name__ == '__main__':
    unittest.main()
