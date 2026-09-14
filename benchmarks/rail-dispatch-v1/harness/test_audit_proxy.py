#!/usr/bin/env python3
"""Offline integration test for audit_proxy using a loopback fake upstream."""
import json
import importlib.util
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

_spec = importlib.util.spec_from_file_location("audit_proxy", Path(__file__).with_name("audit_proxy.py"))
audit_proxy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_proxy)


class FakeUpstream(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.__class__.calls.append((self.path, json.loads(body)))
        response = json.dumps({"id": "fake", "usage": {"prompt_tokens": 11,
            "completion_tokens": 7, "total_tokens": 18,
            "prompt_tokens_details": {"cached_tokens": 3}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


class AuditProxyTest(unittest.TestCase):
    def setUp(self):
        FakeUpstream.calls = []
        self.temp = tempfile.TemporaryDirectory()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        self.audit_path = Path(self.temp.name) / "audit.jsonl"
        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), audit_proxy.ProxyHandler)
        self.proxy.daemon_threads = True
        self.proxy.audit = audit_proxy.AuditWriter(self.audit_path)
        self.proxy.upstream = f"http://127.0.0.1:{self.upstream.server_port}/v1"
        self.proxy.upstream_timeout = 3
        self.proxy.max_request_bytes = 1024 * 1024
        self.proxy.ssl_context = None
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()
        self.base = f"http://127.0.0.1:{self.proxy.server_port}"

    def tearDown(self):
        self.proxy.shutdown(); self.proxy.server_close(); self.proxy.audit.close()
        self.upstream.shutdown(); self.upstream.server_close(); self.temp.cleanup()

    def request(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {} if data is None else {"Content-Type": "application/json"}
        with urlopen(Request(self.base + path, data, headers), timeout=3) as response:
            return response.status, json.loads(response.read())

    def test_allowlist_models_usage_and_rejection(self):
        status, models = self.request("/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual([x["id"] for x in models["data"]], ["gpt-5.6-sol"])
        self.assertEqual(FakeUpstream.calls, [])

        status, _ = self.request("/v1/chat/completions", {"model": "gpt-5.6-sol", "messages": []})
        self.assertEqual(status, 200)
        status, _ = self.request("/responses", {"model": "gpt-5.6-sol", "input": "x"})
        self.assertEqual(status, 200)
        self.assertEqual([x[0] for x in FakeUpstream.calls], ["/v1/chat/completions", "/v1/responses"])

        with self.assertRaises(HTTPError) as caught:
            self.request("/chat/completions", {"model": "other", "messages": []})
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(len(FakeUpstream.calls), 2)

        for _ in range(50):
            rows = [json.loads(line) for line in self.audit_path.read_text().splitlines()]
            if any(row.get("violation") == "model_not_allowed" for row in rows):
                break
            threading.Event().wait(.01)
        usage_rows = [row for row in rows if row.get("usage")]
        self.assertEqual(usage_rows[0]["usage"], {"prompt_tokens": 11, "completion_tokens": 7,
                                                   "total_tokens": 18, "cache_tokens": 3})
        self.assertTrue(any(row.get("violation") == "model_not_allowed" for row in rows))


if __name__ == "__main__":
    unittest.main()
