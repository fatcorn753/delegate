"""agy と Copilot CLI の呼び出しと、出力の共通形式への変換。"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import EDIT

# agy の読み取り専用設定。deny は allow より優先されるため、コマンドは全面禁止にして
# ファイル読み取りと検索系ツールだけで調べさせる(2026-11 時点の agy 1.5 で確認)。
# headless では許可ルールにない要求で実行全体が止まるので、明示的に deny して
# エラーをモデルへ返させる。
AGY_READONLY_SETTINGS = {
    "agentMode": "",
    "permissions": {"deny": ["command(*)", "write_file(*)", "mcp(*)"]},
}


@dataclass
class Result:
    backend: str
    ok: bool
    text: str = ""
    model: str = ""
    duration: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    credits: float | None = None
    error: str = ""
    files_changed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class BackendError(Exception):
    pass


def _brief(s: str, n: int = 600) -> str:
    # agy・copilot とも原因は先頭に出て、後ろはモデル一覧などの補足が続くため先頭を残す
    return s.strip()[:n]


def _run(argv: list[str], cwd: Path, timeout: int) -> tuple[int, str, str, float]:
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except FileNotFoundError as e:
        raise BackendError(f"command not found: {argv[0]}") from e
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s", time.monotonic() - t0
    return p.returncode, p.stdout, p.stderr, time.monotonic() - t0


def build_agy_argv(cmd: str, prompt: str, model: str, mode: str, add_dirs: list[Path], timeout: int) -> list[str]:
    if mode == EDIT:
        raise BackendError("agy backend supports read-only tasks only")
    argv = [
        cmd,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--print-timeout",
        f"{max(timeout - 10, 30)}s",
        "--settings",
        json.dumps(AGY_READONLY_SETTINGS),
    ]
    if model:
        argv += ["--model", model]
    for d in add_dirs:
        argv += ["--add-dir", str(d)]
    return argv


def parse_agy(code: int, out: str, err: str) -> Result:
    r = Result(backend="agy", ok=False)
    try:
        start = out.index("{")
        d = json.loads(out[start:])
    except ValueError:
        r.error = _brief(err or out) or f"exit {code}"
        return r
    usage = d.get("usage") or {}
    r.text = (d.get("response") or "").strip()
    r.tokens_in = int(usage.get("input_tokens") or 0)
    r.tokens_out = int(usage.get("output_tokens") or 0) + int(usage.get("thinking_tokens") or 0)
    status = d.get("status")
    if code == 0 and status == "SUCCESS" and r.text:
        r.ok = True
    else:
        denied = d.get("denied_actions")
        r.error = _brief(err) or f"status={status} exit={code}" + (f" denied={denied}" if denied else "")
    return r


def build_copilot_argv(cmd: str, prompt: str, model: str, mode: str, add_dirs: list[Path]) -> list[str]:
    argv = [cmd, "-p", prompt, "--output-format", "json", "--no-ask-user", "--no-auto-update"]
    if mode == EDIT:
        # 書き込みだけ許可する。シェルの変更系は非対話モードでは許可ルールがない限り拒否される。
        argv += ["--allow-tool", "write", "--deny-tool", "shell(git push)", "--deny-tool", "shell(git commit)"]
    else:
        argv += ["--deny-tool", "write"]
    if model:
        argv += ["--model", model]
    for d in add_dirs:
        argv += ["--add-dir", str(d)]
    return argv


def parse_copilot(code: int, out: str, err: str) -> Result:
    r = Result(backend="copilot", ok=False)
    messages: list[str] = []
    models: list[str] = []
    exit_code = code
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        typ = ev.get("type")
        data = ev.get("data") or {}
        if typ == "assistant.message" and data.get("content"):
            messages.append(data["content"])
            if data.get("model"):
                models.append(data["model"])
        elif typ == "session.usage_checkpoint":
            nano = data.get("totalNanoAiu")
            if nano is not None:
                r.credits = nano / 1e9
            tin = tout = 0
            for m in data.get("providerModelMetrics") or []:
                u = (m.get("metrics") or {}).get("usage") or {}
                tin += int(u.get("inputTokens") or 0)
                tout += int(u.get("outputTokens") or 0)
            if tin or tout:
                r.tokens_in, r.tokens_out = tin, tout
        elif typ == "result":
            exit_code = ev.get("exitCode", exit_code)
            changes = (ev.get("usage") or {}).get("codeChanges") or {}
            r.files_changed = list(changes.get("filesModified") or [])
    r.text = messages[-1].strip() if messages else ""
    r.model = models[-1] if models else ""
    if code == 0 and exit_code == 0 and r.text:
        r.ok = True
    else:
        r.error = _brief(err) or f"exit {code}/{exit_code}, no answer"
    return r


def run(name: str, bcfg: dict, prompt: str, cwd: Path, model: str, mode: str,
        add_dirs: list[Path], timeout: int | None = None) -> Result:
    timeout = int(timeout or bcfg.get("timeout", 600))
    cmd = bcfg.get("command", name)
    if name == "agy":
        argv = build_agy_argv(cmd, prompt, model, mode, add_dirs, timeout)
        code, out, err, dur = _run(argv, cwd, timeout)
        r = parse_agy(code, out, err)
        r.model = r.model or model or "default"
    elif name == "copilot":
        argv = build_copilot_argv(cmd, prompt, model, mode, add_dirs)
        code, out, err, dur = _run(argv, cwd, timeout)
        r = parse_copilot(code, out, err)
    else:
        raise BackendError(f"unknown backend: {name}")
    r.duration = round(dur, 2)
    return r
