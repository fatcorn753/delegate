#!/usr/bin/env python3
"""テスト用の偽 agy / copilot。argv を記録し、FAKE_MODE に応じた出力を返す。

FAKE_MODE: ok | fail | garbage | edit(copilot のみ: cwd の target.txt を書き換える)
"""

import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
log = os.environ.get("FAKE_ARGV_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"name": name, "argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")

mode = os.environ.get(f"FAKE_MODE_{name.upper()}", os.environ.get("FAKE_MODE", "ok"))

if mode == "fail":
    print("quota exceeded", file=sys.stderr)
    sys.exit(1)
if mode == "garbage":
    print("not json at all")
    sys.exit(0)

if name == "agy":
    print(json.dumps({
        "conversation_id": "x",
        "status": "SUCCESS",
        "response": "agy answer\n",
        "duration_seconds": 1.0,
        "usage": {"input_tokens": 100, "output_tokens": 10, "thinking_tokens": 5},
    }))
else:
    if mode == "edit":
        Path("target.txt").write_text("changed\n")
        Path("new.txt").write_text("new\n")
    events = [
        {"type": "assistant.message", "data": {"content": "", "model": "m1"}},
        {"type": "assistant.message", "data": {"content": "copilot answer", "model": "m1"}},
        {"type": "session.usage_checkpoint", "data": {
            "totalNanoAiu": 250000000,
            "providerModelMetrics": [{"metrics": {"usage": {"inputTokens": 200, "outputTokens": 20}}}],
        }},
        {"type": "result", "exitCode": 0, "usage": {"codeChanges": {"filesModified": ["target.txt"] if mode == "edit" else []}}},
    ]
    for e in events:
        print(json.dumps(e))
