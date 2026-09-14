#!/usr/bin/env python3
"""Summarize token usage from audit_proxy JSONL."""
import argparse
import json
import sys
from pathlib import Path

FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "cache_tokens")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Summarize calls and token usage in audit JSONL")
    parser.add_argument("audit", help="audit JSONL file")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    result = {"calls": 0, "models": set(), "violations": 0,
              **{field: 0 for field in FIELDS}, "missing_usage_calls": 0}
    malformed = 0
    with Path(args.audit).expanduser().open(encoding="utf-8") as source:
        for lineno, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                malformed += 1
                print(f"warning: malformed JSON at line {lineno}", file=sys.stderr)
                continue
            result["calls"] += 1
            if isinstance(row.get("model"), str):
                result["models"].add(row["model"])
            # Only a model allowlist breach is a route violation. Upstream/transport
            # failures are operational errors and must not be mislabeled as Astra use.
            if row.get("violation") == "model_not_allowed":
                result["violations"] += 1
            usage = row.get("usage")
            if not isinstance(usage, dict):
                result["missing_usage_calls"] += 1
                continue
            for field in FIELDS:
                value = usage.get(field)
                if isinstance(value, int) and not isinstance(value, bool):
                    result[field] += value
    result["models"] = sorted(result["models"])
    result["malformed_lines"] = malformed
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"calls: {result['calls']}")
        print("models: " + (", ".join(result["models"]) or "(none)"))
        print(f"violations: {result['violations']}")
        for field in FIELDS:
            print(f"{field}: {result[field]}")
        print(f"missing_usage_calls: {result['missing_usage_calls']}")
        if malformed:
            print(f"malformed_lines: {malformed}")


if __name__ == "__main__":
    main()
