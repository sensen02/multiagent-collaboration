#!/usr/bin/env python3
"""Drive staged benchmark revisions through an existing Unison HTTP service."""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TERMINAL_RUN = {"completed", "failed", "cancelled"}
TERMINAL_TASK = {"completed", "failed", "cancelled", "superseded"}


def now():
    return datetime.now(timezone.utc).isoformat()


class Client:
    def __init__(self, base, token, output, request_timeout):
        self.base = base.rstrip("/")
        self.token = token
        self.request_timeout = request_timeout
        self.responses = (output / "responses.jsonl").open("w", encoding="utf-8", buffering=1)
        self.events = (output / "events.jsonl").open("w", encoding="utf-8", buffering=1)
        self.cursor = 0

    def close(self):
        self.responses.close()
        self.events.close()

    def request(self, method, path, data=None, query=None, record=True):
        url = self.base + path + (("?" + urlencode(query)) if query else "")
        body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        started = time.monotonic()
        try:
            with urlopen(Request(url, body, headers, method=method), timeout=self.request_timeout) as response:
                payload = json.loads(response.read())
                status = response.status
        except HTTPError as exc:
            status = exc.code
            raw = exc.read()
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {"error": raw.decode(errors="replace")[:1000]}
            raise RuntimeError(f"{method} {path} returned HTTP {status}: {payload}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"{method} {path} failed: {exc}") from exc
        finally:
            duration = round((time.monotonic() - started) * 1000, 3)
        if record:
            self.responses.write(json.dumps({"time": now(), "method": method, "path": path,
                                              "status": status, "duration_ms": duration,
                                              "response": payload}, ensure_ascii=False) + "\n")
        return payload

    def collect_events(self, run_id):
        rows = self.request("GET", "/api/events", query={"run_id": run_id, "after": self.cursor, "limit": 20000})
        for row in rows:
            self.events.write(json.dumps(row, ensure_ascii=False) + "\n")
            seq = row.get("seq")
            if isinstance(seq, int):
                self.cursor = max(self.cursor, seq)
        return rows


def identify_run(value):
    if not isinstance(value, dict):
        raise RuntimeError("run response is not an object")
    candidate = value.get("run") if isinstance(value.get("run"), dict) else value
    run_id = candidate.get("id") or candidate.get("run_id")
    if not isinstance(run_id, str):
        raise RuntimeError(f"run response has no id: {value}")
    return run_id, candidate


def state_entities(state, run_id):
    runs = state.get("runs", []) if isinstance(state, dict) else []
    tasks = state.get("tasks", []) if isinstance(state, dict) else []
    run = next((r for r in runs if r.get("id") == run_id), None)
    related = [t for t in tasks if t.get("run_id") == run_id]
    return run, related


def wait_stage(client, run_id, timeout, interval):
    deadline = time.monotonic() + timeout
    latest = None
    while True:
        state = client.request("GET", "/api/state")
        run, tasks = state_entities(state, run_id)
        if run is None:
            raise RuntimeError(f"run {run_id} disappeared from /api/state")
        client.collect_events(run_id)
        root_id = run.get("root_task")
        root = next((t for t in tasks if t.get("id") == root_id), None)
        latest = {"run": run, "root_task": root, "task_ids": [t.get("id") for t in tasks]}
        # Runtime sets run=completed only after root and all same-revision children terminate.
        if run.get("status") in TERMINAL_RUN:
            if run.get("status") != "completed":
                raise RuntimeError(f"run {run_id} ended as {run.get('status')}")
            return latest
        if root and root.get("status") in {"failed", "cancelled"}:
            raise RuntimeError(f"root task {root_id} ended as {root.get('status')}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"run {run_id} stage timed out; latest={latest}")
        time.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Drive stage0..3 through an existing Unison server")
    parser.add_argument("stage0", help="initial goal/prompt file")
    parser.add_argument("stage1", help="first revision prompt file")
    parser.add_argument("stage2", help="second revision prompt file")
    parser.add_argument("stage3", help="third revision prompt file")
    parser.add_argument("--url", default="http://127.0.0.1:8740", help="Unison base URL")
    parser.add_argument("--workspace", required=True, help="workspace passed to /api/runs")
    parser.add_argument("--model-id", required=True, help="existing Unison model id")
    parser.add_argument("--token", default=os.environ.get("UNISON_TOKEN", ""), help="API token (or UNISON_TOKEN); never saved")
    parser.add_argument("--output", default="unison-output", help="artifact directory")
    parser.add_argument("--stage-timeout", type=float, default=1800, help="seconds per stage")
    parser.add_argument("--interval", type=float, default=2, help="poll interval seconds")
    parser.add_argument("--request-timeout", type=float, default=90, help="individual HTTP timeout")
    args = parser.parse_args(argv)
    if args.stage_timeout <= 0 or args.interval <= 0 or args.request_timeout <= 0:
        parser.error("timeouts and interval must be positive")
    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    prompts = [Path(x).expanduser().read_text(encoding="utf-8") for x in
               (args.stage0, args.stage1, args.stage2, args.stage3)]
    client = Client(args.url, args.token, output, args.request_timeout)
    summary = {"started_at": now(), "url": args.url, "workspace": str(Path(args.workspace).resolve()),
               "model_id": args.model_id, "stages": []}
    try:
        response = client.request("POST", "/api/runs", {"goal": prompts[0], "workspace": args.workspace,
                                                          "model_id": args.model_id})
        run_id, _ = identify_run(response)
        summary["initial_run_id"] = run_id
        for index in range(4):
            if index:
                response = client.request("POST", "/api/revise", {"run_id": run_id, "goal": prompts[index]})
                new_id, _ = identify_run(response)
                # Current server preserves run id and replaces root_task; tolerate future new-run revisions.
                run_id = new_id
            begun = time.monotonic()
            stage = {"stage": index, "run_id": run_id, "started_at": now()}
            final = wait_stage(client, run_id, args.stage_timeout, args.interval)
            stage.update(finished_at=now(), duration_ms=round((time.monotonic() - begun) * 1000, 3),
                         root_task_id=final["run"].get("root_task"), task_ids=final["task_ids"],
                         run_status=final["run"].get("status"), revision=final["run"].get("revision"))
            summary["stages"].append(stage)
        client.collect_events(run_id)
        final_state = client.request("GET", "/api/state")
        final_run, final_tasks = state_entities(final_state, run_id)
        summary.update(ok=True, final_run_id=run_id, final_root_task_id=final_run.get("root_task") if final_run else None,
                       final_task_ids=[t.get("id") for t in final_tasks], finished_at=now())
    except Exception as exc:
        summary.update(ok=False, error=f"{type(exc).__name__}: {exc}", finished_at=now())
        raise
    finally:
        client.close()
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
