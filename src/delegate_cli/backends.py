"""agy と Copilot CLI の呼び出しと、出力の共通形式への変換。"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import EDIT, GITHUB, IMPLEMENT

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


# Copilot の GitHub 読み取り用 MCP ツール。gh タスクではこれ以外のツール(シェル・ファイル読み取り)を与えない。
GITHUB_READ_TOOLS = [
    "list_issues", "issue_read", "search_issues",
    "list_pull_requests", "pull_request_read", "search_pull_requests",
    "actions_list", "actions_get", "get_job_logs",
    "list_commits", "get_commit", "list_branches", "list_releases", "get_latest_release",
    "get_file_contents", "search_code",
]
GITHUB_TOOLSETS = ["issues", "pull_requests", "actions", "repos"]


def agy_settings(masked: list[Path]) -> dict:
    deny = list(AGY_READONLY_SETTINGS["permissions"]["deny"])
    deny += [f"read_file({p})" for p in masked]
    return {**AGY_READONLY_SETTINGS, "permissions": {"deny": deny}}


# agy に編集させるときに通すコマンドの既定。agy の command(...) は語の前方一致。
# テストの実行は既定に入れない。テストはリポジトリのコードを動かすので、agy が書き換えたテストを
# 通じて任意のコード(秘匿情報の送信など)を実行できてしまうため。静的な検査と読み取り系 git だけにする。
AGY_EDIT_COMMANDS = ["ruff check", "ruff format --check", "pyright", "git diff", "git status"]


def agy_edit_settings(cwd: Path, masked: list[Path], protect: list[Path], commands: list[str]) -> dict:
    """書き込みは作業ディレクトリの中だけ。.git と秘匿ファイルへの書き込みは禁止する。

    deny は allow より優先される。秘匿ファイルがあるリポジトリではシェルを全面禁止にする
    (秘匿ファイルを読めてシェルも使えると、外へ送る経路になるため)。許可にないコマンドを
    headless で求めると実行全体が止まるので、使えるコマンドはプロンプトでも伝える。
    """
    allow = [f"write_file({cwd})"]
    deny = ["mcp(*)", f"write_file({cwd / '.git'})"]
    deny += [f"write_file({p})" for p in protect]
    deny += [f"read_file({p})" for p in masked]
    if protect or not commands:
        deny.append("command(*)")
    else:
        allow += [f"command({c})" for c in commands]
    return {"agentMode": "", "permissions": {"allow": allow, "deny": deny}}


def agy_edit_note(settings: dict) -> str:
    cmds = [a[len("command("):-1] for a in settings["permissions"]["allow"] if a.startswith("command(")]
    if not cmds:
        return "\n\nYou cannot run any shell command. Use the file read/write tools only."
    return ("\n\nShell commands you may run (anything else stops the run, so do not try): "
            + ", ".join(cmds) + ". Run one command at a time; do not chain with ; && | or $( ). "
            "Do not run tests; the caller runs them.")


def build_agy_argv(cmd: str, prompt: str, model: str, mode: str, add_dirs: list[Path], timeout: int,
                   masked: list[Path] | None = None, edit_settings: dict | None = None) -> list[str]:
    if mode == "edit":
        if edit_settings is None:
            raise BackendError("agy edit needs edit settings")
        settings = edit_settings
        prompt += agy_edit_note(edit_settings)
    elif mode == "read":
        settings = agy_settings(masked or [])
    else:
        raise BackendError(f"agy backend supports read and edit tasks only (not {mode})")
    argv = [
        cmd,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--print-timeout",
        f"{max(timeout - 10, 30)}s",
        "--settings",
        json.dumps(settings),
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


def build_copilot_argv(cmd: str, prompt: str, model: str, mode: str, add_dirs: list[Path],
                       masked: list[Path] | None = None, session_id: str = "") -> list[str]:
    """Copilot にはシェルと内容検索ツールを一切与えない。

    2026-11 時点の copilot 1.0.114 で確認した抜け道:
    - read(path) で禁止しても、検索ツール rg や意味検索・サブエージェントは中身を読める
    - 読み取り系のシェルコマンド(cat など)は確認なしで動き、作業ディレクトリ外の絶対パスも読める
    - 一時ディレクトリは既定で読めるので --disallow-temp-dir で閉じる
    そのため使えるツールを許可リストで絞り、秘匿ファイルは read/write とも個別に禁止する。
    """
    argv = [cmd, "-p", prompt, "--output-format", "json", "--no-ask-user", "--no-auto-update",
            "--disallow-temp-dir", "--deny-tool", "shell"]
    if mode == GITHUB:
        for ts in GITHUB_TOOLSETS:
            argv += ["--add-github-mcp-toolset", ts]
        tools = [f"github-mcp-server-{t}" for t in GITHUB_READ_TOOLS]
        argv += ["--deny-tool", "write", "--available-tools", *tools]
        for t in GITHUB_READ_TOOLS:
            argv += ["--allow-tool", f"github-mcp-server({t})"]
    elif mode in (EDIT, IMPLEMENT):
        argv += ["--allow-tool", "write", "--available-tools", "view", "glob", "apply_patch"]
    else:
        argv += ["--deny-tool", "write", "--available-tools", "view", "glob"]
    for p in masked or []:
        argv += ["--deny-tool", f"read({p})", "--deny-tool", f"write({p})"]
    if model:
        argv += ["--model", model]
    for d in add_dirs:
        argv += ["--add-dir", str(d)]
    if session_id:
        # 同じ ID で呼ぶと前回の会話を引き継ぐ。差し戻しのたびに仕様とコードを読み直させないため
        argv += ["--session-id", session_id]
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
        add_dirs: list[Path], timeout: int | None = None, masked: list[Path] | None = None,
        session_id: str = "", protect: list[Path] | None = None, commands: list[str] | None = None) -> Result:
    timeout = int(timeout or bcfg.get("timeout", 600))
    cmd = bcfg.get("command", name)
    if name == "agy":
        es = None
        if mode == EDIT:
            es = agy_edit_settings(cwd, masked or [], protect or [],
                                   AGY_EDIT_COMMANDS if commands is None else commands)
        argv = build_agy_argv(cmd, prompt, model, mode, add_dirs, timeout, masked, es)
        code, out, err, dur = _run(argv, cwd, timeout)
        r = parse_agy(code, out, err)
        r.model = r.model or model or "default"
    elif name == "copilot":
        argv = build_copilot_argv(cmd, prompt, model, mode, add_dirs, masked, session_id)
        code, out, err, dur = _run(argv, cwd, timeout)
        r = parse_copilot(code, out, err)
    else:
        raise BackendError(f"unknown backend: {name}")
    r.duration = round(dur, 2)
    return r
