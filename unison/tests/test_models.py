import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unison.models import Models
from unison.store import Store
from unison.config import import_codex_config, import_provider_config, normalize_provider_config

DEFAULT_EVENT_STREAM = "\n".join([
    'event: response.created',
    'data: {"type":"response.created","response":{"id":"resp_sse","status":"in_progress"}}',
    '',
    'event: response.output_item.done',
    'data: {"type":"response.output_item.done","item":{"type":"message","role":"assistant",'
    '"content":[{"type":"output_text","text":"流式答复"}]}}',
    '',
    'event: response.completed',
    'data: {"type":"response.completed","response":{"id":"resp_sse","status":"completed",'
    '"output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"流式答复"}]}],'
    '"usage":{"input_tokens":11,"output_tokens":3}}}',
    '',
    'data: [DONE]',
    '',
])


class Endpoint(BaseHTTPRequestHandler):
    requests=[]
    user_agents=[]
    status=200
    mode='chat'
    error_body=None      # 非 200 时返回的响应体（用于验证上游原话是否被保留/脱敏）
    stream=False         # True 时按 text/event-stream 返回（复现网关无视 stream:false 的行为）
    stream_body=''       # 自定义 SSE 文本；留空则用内置的默认流
    models_body={'data':[{'id':'fixture-a'},{'id':'acme/fixture-b'}]}
    default_leaderboard='''<!doctype html><table><thead><tr><th>Model</th><th>Organization</th><th>Overall</th><th>Reasoning</th><th>Coding</th><th>Agent</th></tr></thead><tbody><tr><td><a href="/models/fixture-a">Fixture A</a></td><td>Fixture Org</td><td>88.5</td><td>90</td><td>87</td><td>86</td></tr><tr><td><a href="/models/fixture-b">Fixture B</a></td><td>Acme</td><td>75</td><td>76</td><td>77</td><td>78</td></tr></tbody></table>'''
    leaderboard=default_leaderboard
    def log_message(self,*args): pass
    def do_GET(self):
        type(self).requests.append((self.path,None,self.headers.get('Authorization')))
        if self.path=='/leaderboard':
            body=type(self).leaderboard.encode()
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
        else:
            body=json.dumps(type(self).models_body).encode()
            self.send_response(type(self).status);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_POST(self):
        payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        type(self).requests.append((self.path,payload,self.headers.get('Authorization')))
        type(self).user_agents.append(self.headers.get('User-Agent',''))
        if type(self).mode=='responses':
            response={'status':'completed','output':[{'type':'function_call','id':'fc_1','call_id':'call_response','name':'workspace_list','arguments':'{}'}],
                      'usage':{'input_tokens':40,'output_tokens':2}}
        else:
            response={'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{'id':'call','type':'function','function':{'name':'workspace_list','arguments':'{}'}}]},'finish_reason':'tool_calls'}],
                      'usage':{'prompt_tokens':42,'prompt_cache_hit_tokens':30,'prompt_cache_miss_tokens':12}}
        if type(self).error_body is not None and type(self).status>=400:
            body=json.dumps(type(self).error_body).encode()
        elif type(self).stream:
            body=(type(self).stream_body or DEFAULT_EVENT_STREAM).encode()
            self.send_response(type(self).status)
            self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            return
        else:
            body=json.dumps(response).encode()
        self.send_response(type(self).status);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)

class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Endpoint.requests=[];Endpoint.user_agents=[];Endpoint.status=200;Endpoint.mode='chat';Endpoint.error_body=None;Endpoint.stream=False;Endpoint.stream_body='';Endpoint.models_body={'data':[{'id':'fixture-a'},{'id':'acme/fixture-b'}]};Endpoint.leaderboard=Endpoint.default_leaderboard
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name);self.models=Models(self.store)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Endpoint)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.models.save({'id':'local','model':'fixture','base_url':f'http://127.0.0.1:{self.server.server_port}/v1','api_key':'test-fixture-only','context_window':32000,'max_output':4096})
    async def asyncTearDown(self):
        await asyncio.to_thread(self.server.shutdown);self.server.server_close();self.store.close();self.temp.cleanup()
    async def test_real_http_tool_schema_and_usage(self):
        msg,usage=await self.models.call('local',[{'role':'user','content':'hi'}],[{'type':'function','function':{'name':'workspace_list','parameters':{'type':'object'}}}])
        self.assertEqual(msg['tool_calls'][0]['function']['name'],'workspace_list')
        path,payload,auth=Endpoint.requests[0]
        self.assertEqual(path,'/v1/chat/completions');self.assertEqual(auth,'Bearer test-fixture-only')
        self.assertEqual(payload['model'],'fixture');self.assertEqual(usage['prompt_cache_hit_tokens'],30)
        self.assertNotIn('api_key',self.models.list()[0])
    async def test_responses_wire_api_converts_tools_and_output(self):
        Endpoint.mode='responses'
        self.models.save({'id':'responses','model':'gpt-5.5','base_url':f'http://127.0.0.1:{self.server.server_port}/v1',
                          'api_key':'response-key','adapter':'responses','disable_response_storage':True,
                          'context_window':32000,'max_output':4096})
        history=[{'role':'assistant','content':None,'tool_calls':[{'id':'prior','type':'function','function':{'name':'workspace_read','arguments':'{"path":"a"}'}}]},
                 {'role':'tool','tool_call_id':'prior','content':'result'}]
        msg,usage=await self.models.call('responses',history,[{'type':'function','function':{'name':'workspace_list','description':'list','parameters':{'type':'object'}}}])
        path,payload,auth=Endpoint.requests[-1]
        self.assertEqual(path,'/v1/responses');self.assertEqual(auth,'Bearer response-key')
        self.assertFalse(payload['store']);self.assertEqual(payload['tools'][0]['name'],'workspace_list')
        self.assertEqual(payload['input'][0]['type'],'function_call');self.assertEqual(payload['input'][1]['type'],'function_call_output')
        self.assertEqual(msg['tool_calls'][0]['id'],'call_response');self.assertEqual(usage['input_tokens'],40)

    async def test_http_failure_does_not_echo_body(self):
        Endpoint.status=401
        with self.assertRaisesRegex(RuntimeError,'HTTP 401'):
            await self.models.call('local',[{'role':'user','content':'hi'}])
    async def test_missing_key_explicit_failure(self):
        self.models.save({'id':'missing','model':'fixture','base_url':'http://127.0.0.1:1/v1'})
        with self.assertRaisesRegex(ValueError,'API Key'): await self.models.call('missing',[])

    async def test_discovery_upserts_all_ids_and_inherits_provider(self):
        result=self.models.discover({'provider_id':'fixture','provider_name':'Fixture Provider',
            'base_url':f'http://127.0.0.1:{self.server.server_port}/v1','api_key':'discover-key','adapter':'responses'})
        self.assertEqual(result['discovered'],2);self.assertIsNone(result['discovery_error'])
        model=self.store.get('model','fixture:fixture-a')
        self.assertEqual(model['model'],'fixture-a');self.assertEqual(model['adapter'],'responses')
        self.assertEqual(model['api_key'],'discover-key');self.assertEqual(model['base_url'],f'http://127.0.0.1:{self.server.server_port}/v1')
        self.assertIn(('/v1/models',None,'Bearer discover-key'),Endpoint.requests)

    async def test_discovery_accepts_top_level_models_and_list(self):
        for key in ('models','list'):
            Endpoint.models_body={key:[{'id':f'{key}-model'}]}
            result=self.models.discover({'provider_id':key,'base_url':f'http://127.0.0.1:{self.server.server_port}'})
            self.assertEqual(result['models'][0]['model'],f'{key}-model')

    async def test_benchmark_fixture_matches_and_uses_24_hour_cache(self):
        self.models.discover({'provider_id':'fixture','base_url':f'http://127.0.0.1:{self.server.server_port}/v1'})
        url=f'http://127.0.0.1:{self.server.server_port}/leaderboard'
        first=self.models.refresh_benchmarks(url,force=True)
        self.assertFalse(first['cached']);self.assertEqual(first['entries'],2);self.assertEqual(first['matched'],2)
        benchmark=self.store.get('model','fixture:fixture-a')['benchmark']
        self.assertEqual(benchmark['overall'],88.5);self.assertEqual(benchmark['reasoning'],90.0)
        self.assertEqual(benchmark['source'],'LLM Stats');self.assertEqual(benchmark['attribution_url'],'https://llm-stats.com/')
        request_count=len(Endpoint.requests)
        second=self.models.refresh_benchmarks(url)
        self.assertTrue(second['cached']);self.assertEqual(len(Endpoint.requests),request_count)

    async def test_ambiguous_leaf_alias_is_not_matched(self):
        Endpoint.leaderboard='''<table><tr><th>Model</th><th>Overall</th></tr><tr><td><a href="/models/org-a/same">Same</a></td><td>80</td></tr><tr><td><a href="/models/org-b/same">Same 2</a></td><td>70</td></tr></table>'''
        self.models.save({'id':'wire','model':'provider/same','base_url':'http://example.test/v1','no_key':True})
        result=self.models.refresh_benchmarks(f'http://127.0.0.1:{self.server.server_port}/leaderboard',force=True)
        self.assertEqual(result['matched'],0);self.assertNotIn('benchmark',self.store.get('model','wire'))

    async def test_failed_benchmark_refresh_preserves_model_and_scores(self):
        self.models.save({'id':'scored','model':'fixture-a','base_url':'http://example.test/v1','no_key':True})
        good=f'http://127.0.0.1:{self.server.server_port}/leaderboard'
        self.models.refresh_benchmarks(good,force=True)
        result=self.models.refresh_benchmarks('http://127.0.0.1:1/unavailable',force=True)
        self.assertTrue(result['refresh_error']);self.assertEqual(self.store.get('model','scored')['benchmark']['overall'],88.5)

    async def test_import_codex_toml_creates_primary_review_and_config(self):
        config=import_codex_config(self.store,self.models,f'''
model_provider = "OpenAI"
model = "gpt-5.5"
review_model = "gpt-5.5-review"
disable_response_storage = true
model_catalog_json = "%NOT_SET%/missing.json"
network_access = "enabled"
windows_wsl_setup_acknowledged = true
[model_providers.OpenAI]
name = "OpenAI"
base_url = "http://127.0.0.1:{self.server.server_port}/v1"
wire_api = "responses"
requires_openai_auth = true
[features]
goals = true
''','plain-key')
        self.assertEqual(config['model_id'],'OpenAI:gpt-5.5')
        self.assertEqual(config['review_model_id'],'OpenAI:gpt-5.5-review')
        self.assertTrue(config['features']['goals']);self.assertTrue(config['catalog_error'])
        primary=self.store.get('model','OpenAI:gpt-5.5')
        self.assertEqual(primary['adapter'],'responses');self.assertTrue(primary['disable_response_storage'])
        # 用户随配置导入的 key 优先；源码里不再有内置 Key（只从 UNISON_BUILTIN_API_KEY 读）
        self.assertEqual(primary['api_key'],'plain-key')

    async def test_catalog_metadata_applies_context_limits(self):
        from unittest.mock import patch
        with patch.dict('os.environ',{'CATALOG_HOME':self.temp.name}):
            path=__import__('pathlib').Path(self.temp.name)/'models.json'
            path.write_text(json.dumps({'models':[{'id':'gpt-x','context_window':100000,'max_output_tokens':12000,'description':'catalog model'}]}))
            config=import_codex_config(self.store,self.models,f'''
model_provider = "OpenAI"
model = "gpt-x"
model_catalog_json = "%CATALOG_HOME%/models.json"
[model_providers.OpenAI]
base_url = "http://127.0.0.1:{self.server.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
[features]
goals = true
''')
        model=self.store.get('model',config['model_id'])
        self.assertEqual(model['context_window'],100000);self.assertEqual(model['max_output'],12000)
        self.assertEqual(model['description'],'catalog model');self.assertTrue(model['no_key'])

    async def test_import_dsh_standard_provider_yaml_normalizes_profile(self):
        config=import_provider_config(self.store,self.models,f'''
llm-pi-ai:
  providers:
    acme-gateway:
      displayName: Acme Gateway
      apiKeyEnv: ACME_GATEWAY_API_KEY
      api: openai-responses
      baseURL: http://127.0.0.1:{self.server.server_port}/v1
      defaultContextWindow: 100000
      defaultMaxTokens: 12000
      headers:
        X-Tenant: fixture
      models:
        - id: gpt-main
          name: GPT Main
        - id: gpt-review
          contextWindow: 64000
          maxTokens: 8000
''','dsh-key')
        self.assertEqual(config['format'],'dsh-settings-yaml')
        self.assertEqual(config['source_format'],'dsh-settings-yaml')
        self.assertEqual(config['provider']['api'],'openai-responses')
        self.assertEqual(config['model_id'],'acme-gateway:gpt-main')
        model=self.store.get('model','acme-gateway:gpt-main')
        self.assertEqual(model['adapter'],'responses');self.assertEqual(model['context_window'],100000)
        self.assertEqual(model['max_output'],12000);self.assertEqual(model['headers']['X-Tenant'],'fixture')
        # 扫描用的是本次导入提供的 key
        self.assertIn(('/v1/models',None,'Bearer dsh-key'),Endpoint.requests)

    async def test_discovery_preserves_declared_default_model(self):
        # /models 返回顺序与声明顺序不同；扫描不得改写声明的默认模型
        Endpoint.models_body={'data':[{'id':'newest'},{'id':'declared'}]}
        config=import_provider_config(self.store,self.models,f'''
llm-pi-ai:
  providers:
    acme:
      displayName: Acme
      api: openai-responses
      baseURL: http://127.0.0.1:{self.server.server_port}/v1
      models:
        - id: declared
        - id: newest
agent-default-model:
  provider: acme
  model: declared
''')
        self.assertEqual(config['model_id'],'acme:declared')
        # 已声明的模型排在前，扫描结果追加在后
        self.assertEqual(config['discovered_models'],['acme:declared','acme:newest'])
        self.assertEqual(self.store.get('provider','acme')['default_model'],'declared')
        self.assertTrue(self.store.get('model','acme:declared')['default_model'])
        self.assertFalse(self.store.get('model','acme:newest')['default_model'])

    async def test_import_supports_codex_chat_and_rejects_anthropic_dsh_api(self):
        detected,provider,_=normalize_provider_config('''model_provider="x"\nmodel="m"\n[model_providers.x]\nbase_url="https://example.test/v1"\nwire_api="chat_completions"''')
        self.assertEqual(detected,'codex-toml');self.assertEqual(provider['adapter'],'chat-completions')
        with self.assertRaisesRegex(ValueError,'anthropic-messages.*不支持'):
            normalize_provider_config('''llm-pi-ai:\n  providers:\n    x:\n      api: anthropic-messages\n      baseURL: https://example.test\n      models:\n        - id: m''')

    async def test_actual_dsh_settings_style_uses_default_and_explicit_limits(self):
        config=import_provider_config(self.store,self.models,f'''agent-default-model:
  provider: a
  model: gpt-5.5
permission:
  defaultPreset: danger-full-access
llm-pi-ai:
  providers:
    a:
      displayName: "MiMi OpenAI Proxy"
      apiKeyEnv: A_API_KEY
      api: openai-completions
      baseURL: http://127.0.0.1:{self.server.server_port}/v1
      models:
        - id: gpt-other
          name: GPT Other
        - id: gpt-5.5
          name: GPT-5.5
          contextWindow: 128000
          maxTokens: 16000
''')
        self.assertEqual(config['format'],'dsh-settings-yaml')
        self.assertEqual(config['model_id'],'a:gpt-5.5')
        imported=self.store.get('model','a:gpt-5.5')
        self.assertEqual(imported['adapter'],'chat-completions')
        self.assertEqual(imported['context_window'],128000);self.assertEqual(imported['max_output'],16000)

    async def test_import_error_explains_unrecognized_input(self):
        with self.assertRaisesRegex(ValueError,'无法识别配置|缺少 llm-pi-ai.providers'):
            normalize_provider_config('not a supported configuration')

    async def test_import_dsh_requires_exactly_one_provider(self):
        with self.assertRaisesRegex(ValueError,'一次请粘贴一个'):
            normalize_provider_config('''llm-pi-ai:\n  providers:\n    one:\n      api: openai-responses\n      baseURL: https://one.test\n      models:\n        - id: m\n    two:\n      api: openai-completions\n      baseURL: https://two.test\n      models:\n        - id: m''')

    async def test_unified_call_dispatches_by_provider_api(self):
        # 只有 api、没有 adapter 的记录也必须能解析到唯一执行路径
        record={'id':'api-only','model':'fixture','base_url':f'http://127.0.0.1:{self.server.server_port}/v1',
                'api':'openai-responses','api_key':'api-key','adapter':'bogus','context_window':32000,'max_output':4096}
        self.store.put('model',record)
        Endpoint.mode='responses'
        msg,usage=await self.models.call('api-only',[{'role':'user','content':'hi'}])
        path,payload,auth=Endpoint.requests[-1]
        self.assertEqual(path,'/v1/responses');self.assertEqual(msg['tool_calls'][0]['function']['name'],'workspace_list')

    async def test_unified_errors_carry_stable_codes(self):
        from unison.models import ModelError
        for status,code in ((401,'AUTH'),(429,'RATE_LIMIT'),(400,'INVALID_REQUEST'),(413,'CONTEXT_WINDOW_EXCEEDED')):
            Endpoint.status=status
            with self.assertRaises(ModelError) as caught:
                await self.models.call('local',[{'role':'user','content':'hi'}])
            self.assertEqual(caught.exception.code,code)
            self.assertEqual(caught.exception.status,status)
            # 兼容旧的 RuntimeError/ValueError 捕获方式
            self.assertIsInstance(caught.exception,RuntimeError)


    async def test_event_stream_response_is_parsed(self):
        """真机发现：同一 base_url 换个 Key 之后，网关即使收到 stream:false 也回 SSE。

        于是只认 JSON 的解析器报 MALFORMED_RESPONSE——**连本来可用的模型也一起失效**。
        模型返回一个真实可用的新模型 gpt-6-astra，却被这条缺陷挡在门外。
        """
        from unison.models import ModelError
        Endpoint.mode='responses'; Endpoint.stream=True
        self.models.save({'id':'responses','model':'gpt-6-astra','base_url':f'http://127.0.0.1:{self.server.server_port}/v1',
                          'api_key':'response-key','adapter':'responses','disable_response_storage':True,
                          'context_window':32000,'max_output':4096})
        msg,usage=await self.models.call('responses',[{'role':'user','content':'hi'}])
        self.assertEqual(msg['content'],'流式答复')
        self.assertEqual(usage.get('output_tokens'),3)

    async def test_event_stream_without_completed_event_still_parses(self):
        """网关只发 output_item.done 时也要能拼出结果。"""
        Endpoint.mode='responses'; Endpoint.stream=True
        self.models.save({'id':'responses','model':'gpt-6-astra','base_url':f'http://127.0.0.1:{self.server.server_port}/v1',
                          'api_key':'response-key','adapter':'responses','disable_response_storage':True,
                          'context_window':32000,'max_output':4096})
        Endpoint.stream_body="\n".join([
            'data: {"type":"response.output_item.done","item":{"type":"message","role":"assistant",'
            '"content":[{"type":"output_text","text":"退路"}]}}','',
            'data: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"c1",'
            '"name":"workspace_list","arguments":"{}"}}',''])
        msg,usage=await self.models.call('responses',[{'role':'user','content':'hi'}])
        self.assertEqual(msg['content'],'退路')
        self.assertEqual(msg['tool_calls'][0]['function']['name'],'workspace_list')

    async def test_event_stream_without_any_response_is_an_error(self):
        """流里什么都没有时必须明确报错，不能返回空消息。"""
        from unison.models import ModelError
        Endpoint.mode='responses'; Endpoint.stream=True
        self.models.save({'id':'responses','model':'gpt-6-astra','base_url':f'http://127.0.0.1:{self.server.server_port}/v1',
                          'api_key':'response-key','adapter':'responses','disable_response_storage':True,
                          'context_window':32000,'max_output':4096})
        Endpoint.stream_body='event: ping\ndata: [DONE]\n\n'
        with self.assertRaises(ModelError) as caught:
            await self.models.call('responses',[{'role':'user','content':'hi'}])
        self.assertEqual(caught.exception.code,'MALFORMED_RESPONSE')

    async def test_account_level_400_is_permanent_and_keeps_upstream_words(self):
        """真机发现：同为 400，账户级"不支持"与参数错误必须分开。

        上游实测原话（gpt-5.4 / gpt-5.3-codex-spark）：
        "The 'gpt-5.4' model is not supported when using Codex with a ChatGPT account."
        这是永久限制，重试无用；旧实现把它归成 INVALID_REQUEST('neither')，
        且只用 e.reason 拼消息，把上游原话整段丢掉，人和模型都看不出该换模型。
        """
        from unison.models import ModelError, health_verdict
        Endpoint.status=400
        Endpoint.error_body={'error':{'message':"The 'gpt-5.4' model is not supported when using Codex with a ChatGPT account.",
                                      'type':'invalid_request_error'}}
        try:
            with self.assertRaises(ModelError) as caught:
                await self.models.call('local',[{'role':'user','content':'hi'}])
            self.assertEqual(caught.exception.code,'MODEL_NOT_SUPPORTED')
            self.assertIn('not supported',str(caught.exception))
            self.assertEqual(health_verdict('MODEL_NOT_SUPPORTED')[0],'permanent')
        finally:
            Endpoint.error_body=None

    async def test_upstream_message_is_kept_but_credentials_are_redacted(self):
        """保留上游原话，但绝不回显可能的凭据。"""
        from unison.models import ModelError
        Endpoint.status=400
        Endpoint.error_body={'error':{'message':'bad request with key sk-ABCDEF0123456789 and Bearer abcdefghijklmnop'}}
        try:
            with self.assertRaises(ModelError) as caught:
                await self.models.call('local',[{'role':'user','content':'hi'}])
            text=str(caught.exception)
            self.assertIn('bad request with key',text)
            self.assertNotIn('sk-ABCDEF0123456789',text)
            self.assertNotIn('abcdefghijklmnop',text)
            self.assertIn('已脱敏',text)
        finally:
            Endpoint.error_body=None

    async def test_model_call_sends_stable_user_agent(self):
        # 部分网关拒绝库默认 UA（Python-urllib/x.y）：调用必须带稳定标识
        await self.models.call('local',[{'role':'user','content':'hi'}])
        agent=Endpoint.user_agents[-1]
        self.assertTrue(agent);self.assertNotIn('Python-urllib',agent)

    async def test_missing_key_reports_code(self):
        from unison.models import ModelError
        self.models.save({'id':'nokey','model':'fixture','base_url':'http://127.0.0.1:1/v1'})
        with self.assertRaises(ModelError) as caught:
            await self.models.call('nokey',[])
        self.assertEqual(caught.exception.code,'MISSING_CREDENTIAL')

    async def test_builtin_provider_stays_on_responses_api(self):
        # 守护内置 provider：aaa.bi + Responses 特殊接口不得被改写
        from unison.config import BUILTIN_CONFIG_TOML
        detected,provider,metadata=normalize_provider_config(BUILTIN_CONFIG_TOML)
        self.assertEqual(detected,'codex-toml')
        self.assertEqual(provider['id'],'OpenAI')
        self.assertEqual(provider['base_url'],'https://aaa.bi')
        self.assertEqual(provider['api'],'openai-responses')
        self.assertEqual(provider['adapter'],'responses')
        self.assertTrue(provider['requires_openai_auth'])
        # 主调度默认模型：当前 group 可调用的最优模型（gpt-6-astra 目录里 priority 更高但返回 404）
        self.assertEqual(metadata['model'],'gpt-5.6-sol')


if __name__ == '__main__':
    unittest.main()
