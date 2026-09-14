#!/usr/bin/env python3
"""Local, auditable OpenAI-compatible reverse proxy (standard library only)."""
from __future__ import annotations

import argparse
import json
import os
import ssl
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ALLOWED_MODEL = "gpt-5.6-sol"
ALLOWED_PATHS = {"/chat/completions", "/responses", "/models"}
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def usage_from_obj(obj):
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    details = usage.get("prompt_tokens_details", usage.get("input_tokens_details"))
    details = details if isinstance(details, dict) else {}
    cache = usage.get("cache_tokens")
    if cache is None:
        cache = details.get("cached_tokens")
    if cache is None:
        vals = [usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens")]
        present = [v for v in vals if integer(v) is not None]
        cache = sum(present) if present else None
    return {
        "prompt_tokens": integer(usage.get("prompt_tokens", usage.get("input_tokens"))),
        "completion_tokens": integer(usage.get("completion_tokens", usage.get("output_tokens"))),
        "total_tokens": integer(usage.get("total_tokens")),
        "cache_tokens": integer(cache),
    }


def parse_usage(body: bytes, content_type: str):
    candidates = []
    if "text/event-stream" in content_type.lower() or body.lstrip().startswith(b"data:"):
        for raw in body.splitlines():
            if raw.startswith(b"data:"):
                data = raw[5:].strip()
                if data and data != b"[DONE]":
                    try:
                        candidates.append(json.loads(data))
                    except (ValueError, UnicodeDecodeError):
                        pass
    else:
        try:
            candidates.append(json.loads(body))
        except (ValueError, UnicodeDecodeError):
            pass
    found = None
    for item in candidates:
        parsed = usage_from_obj(item)
        if parsed is not None:
            found = parsed
    return found


class AuditWriter:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a", encoding="utf-8", buffering=1)
        self.lock = threading.Lock()

    def write(self, record):
        # Deliberately record only metadata: never headers, API keys, or prompt bodies.
        with self.lock:
            self.file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def close(self):
        with self.lock:
            self.file.close()


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        started = time.monotonic()
        timestamp = utc_now()
        raw_path = urlsplit(self.path).path
        path = raw_path[3:] if raw_path.startswith("/v1/") else raw_path
        request_bytes = 0
        model = None
        violation = None
        status = 500
        response_body = b""
        response_type = ""
        try:
            if path not in ALLOWED_PATHS:
                status, violation = 404, "path_not_allowed"
                self._send_json(status, {"error": "path not allowed"})
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length < 0 or length > self.server.max_request_bytes:
                status, violation = 413, "request_too_large"
                self._send_json(status, {"error": "request too large"})
                return
            body = self.rfile.read(length) if length else b""
            request_bytes = len(body)
            if path in {"/chat/completions", "/responses"}:
                if self.command != "POST" or "application/json" not in self.headers.get("Content-Type", "").lower():
                    status, violation = 415, "json_required"
                    self._send_json(status, {"error": "JSON POST required"})
                    return
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    status, violation = 400, "invalid_json"
                    self._send_json(status, {"error": "invalid JSON"})
                    return
                model = payload.get("model") if isinstance(payload, dict) else None
                if model != ALLOWED_MODEL:
                    status, violation = 403, "model_not_allowed"
                    self._send_json(status, {"error": "model not allowed"})
                    return
            elif path == "/models":
                if self.command != "GET":
                    status, violation = 405, "method_not_allowed"
                    self._send_json(status, {"error": "method not allowed"})
                    return
                status = 200
                self._send_json(status, {"object": "list", "data": [
                    {"id": ALLOWED_MODEL, "object": "model", "owned_by": "audit"}
                ]})
                return

            upstream_url = self.server.upstream + path
            if urlsplit(self.path).query:
                upstream_url += "?" + urlsplit(self.path).query
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
            headers["Accept-Encoding"] = "identity"
            req = Request(upstream_url, data=body if self.command != "GET" else None,
                          headers=headers, method=self.command)
            try:
                upstream = urlopen(req, timeout=self.server.upstream_timeout,
                                   context=self.server.ssl_context)
            except HTTPError as exc:
                upstream = exc
            with upstream:
                status = upstream.status
                response_body = upstream.read()
                response_type = upstream.headers.get("Content-Type", "")
                self.send_response(status)
                for key, value in upstream.headers.items():
                    if key.lower() not in HOP_HEADERS:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)
        except (URLError, TimeoutError, OSError) as exc:
            status = 502
            violation = violation or "upstream_error"
            try:
                self._send_json(status, {"error": "upstream unavailable", "type": type(exc).__name__})
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            usage = parse_usage(response_body, response_type)
            record = {
                "time": timestamp, "model": model, "path": path, "http_status": status,
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "request_bytes": request_bytes, "violation": violation,
            }
            if usage is not None:
                record["usage"] = usage
            self.server.audit.write(record)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Audit-only local OpenAI-compatible proxy")
    parser.add_argument("--port", type=int, required=True, help="local TCP port to listen on")
    parser.add_argument("--host", default="127.0.0.1", help="listen host (default: 127.0.0.1)")
    parser.add_argument("--audit", default="audit.jsonl", help="append-only JSONL audit path")
    parser.add_argument("--upstream", default="https://aaa.bi/v1", help="upstream base URL")
    parser.add_argument("--timeout", type=float, default=300, help="upstream timeout seconds")
    parser.add_argument("--max-request-bytes", type=int, default=64 * 1024 * 1024)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or args.timeout <= 0 or args.max_request_bytes <= 0:
        parser.error("invalid port, timeout, or request-size limit")
    upstream = args.upstream.rstrip("/")
    parsed = urlsplit(upstream)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        parser.error("--upstream must be an http or https URL")
    audit = AuditWriter(Path(args.audit).expanduser())
    server = ThreadingHTTPServer((args.host, args.port), ProxyHandler)
    server.daemon_threads = True
    server.audit = audit
    server.upstream = upstream
    server.upstream_timeout = args.timeout
    server.max_request_bytes = args.max_request_bytes
    server.ssl_context = ssl.create_default_context()
    print(f"audit proxy listening on http://{args.host}:{args.port}; audit={args.audit}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        audit.close()


if __name__ == "__main__":
    main()
