"""delegate: 作業を agy / Copilot CLI に安全に委任する薄いラッパー。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, backends, guard, runlog
from .config import EDIT, ConfigError, config_path, load, log_path

EXIT_OK, EXIT_FAIL, EXIT_GUARD, EXIT_USAGE = 0, 1, 2, 64


def _err(msg: str) -> None:
    print(f"[delegate] {msg}", file=sys.stderr)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _untracked(cwd: Path) -> set[str]:
    p = _git(cwd, "ls-files", "--others", "--exclude-standard")
    return set(p.stdout.splitlines())


def _edit_precheck(cwd: Path) -> set[str]:
    """編集タスクは git 管理下かつ追跡ファイルに未コミット変更がないときだけ実行する。
    変更を委任先の仕業だと特定できるようにするため。"""
    if _git(cwd, "rev-parse", "--is-inside-work-tree").returncode != 0:
        raise guard.GuardError("edit requires a git repository (so changes can be reviewed and reverted)")
    dirty = _git(cwd, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        raise guard.GuardError("edit requires no uncommitted changes to tracked files:\n" + dirty)
    return _untracked(cwd)


def _edit_report(cwd: Path, before_untracked: set[str]) -> str:
    stat = _git(cwd, "diff", "--stat").stdout.rstrip()
    new = sorted(_untracked(cwd) - before_untracked)
    parts = []
    if stat:
        parts.append(stat)
    if new:
        parts.append("new untracked files:\n  " + "\n  ".join(new))
    return "\n".join(parts) or "no file changes"


def _read_prompt(args: argparse.Namespace) -> str:
    prompt = args.prompt
    # 標準入力は '-' を明示したときだけ読む。呼び出し元によっては stdin が開いたままで、
    # 暗黙に読むと止まってしまうため。
    if prompt == "-":
        prompt = sys.stdin.read()
    return (prompt or "").strip()


def _compose(preamble: str, prompt: str, files: list[Path]) -> str:
    body = prompt
    if files:
        listed = "\n".join(f"- {f}" for f in files)
        body = f"Files:\n{listed}\n\n{prompt or 'Summarize these files.'}"
    return f"{preamble}\n\n{body}" if preamble else body


def cmd_run(args: argparse.Namespace, cfg: dict) -> int:
    task = cfg["tasks"].get(args.task)
    if task is None:
        _err(f"unknown task {args.task!r}; known: {', '.join(cfg['tasks'])}")
        return EXIT_USAGE
    cwd = Path(os.path.realpath(Path(args.C).expanduser()))
    if not cwd.is_dir():
        _err(f"not a directory: {cwd}")
        return EXIT_USAGE
    files = [Path(os.path.realpath(Path(f).expanduser())) for f in (args.files or [])]
    missing = [f for f in files if not f.exists()]
    if missing:
        _err("file not found: " + ", ".join(map(str, missing)))
        return EXIT_USAGE
    prompt = _read_prompt(args)
    if not prompt and not files:
        _err("empty prompt")
        return EXIT_USAGE

    mode = task.get("mode")
    add_dirs = sorted({f.parent for f in files if not str(f).startswith(str(cwd) + os.sep)})
    add_dirs += [Path(os.path.realpath(Path(d).expanduser())) for d in (args.add_dir or [])]

    try:
        guard.check(cwd, [*files, *add_dirs], cfg["guard"])
        before = _edit_precheck(cwd) if mode == EDIT else set()
    except guard.GuardError as e:
        _err(f"refused: {e}")
        return EXIT_GUARD

    full_prompt = _compose(task.get("preamble", ""), prompt, files)
    chain = [args.backend or task["backend"]]
    if not args.backend and not args.no_fallback and task.get("fallback"):
        chain.append(task["fallback"])

    results = []
    for i, name in enumerate(chain):
        # --model は最初の委任先にだけ効かせる。モデル名は委任先ごとに違うので、
        # フォールバック先へ渡すと両方失敗するため(フォールバック先は既定モデル)。
        if i == 0:
            model = args.model if args.model is not None else (task.get("model", "") if name == task["backend"] else "")
        else:
            model = ""
        try:
            r = backends.run(name, cfg["backends"][name], full_prompt, cwd, model, mode, add_dirs, args.timeout)
        except backends.BackendError as e:
            r = backends.Result(backend=name, ok=False, error=str(e), model=model)
        results.append(r)
        runlog.append(log_path(), {
            "ts": runlog.now(),
            "task": args.task,
            "backend": name,
            "model": r.model,
            "cwd": str(cwd),
            "ok": r.ok,
            "duration": r.duration,
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
            "credits": r.credits,
            "fallback_of": chain[0] if i else None,
            "prompt_chars": len(full_prompt),
            "error": r.error[:300],
        })
        if r.ok:
            break
        if i + 1 < len(chain):
            first = r.error.strip().splitlines()[0] if r.error.strip() else "no answer"
            _err(f"{name} failed ({first}); falling back to {chain[i + 1]}")

    final = results[-1]
    edit_report = _edit_report(cwd, before) if mode == EDIT else None

    if args.json:
        payload = final.to_dict() | {"task": args.task, "attempts": [r.backend for r in results]}
        if edit_report is not None:
            payload["edit_report"] = edit_report
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if final.text:
            print(final.text)
        if edit_report is not None:
            print("\n--- changes ---\n" + edit_report)
        cr = "" if final.credits is None else f" credits={final.credits:.3f}"
        _err(f"{final.backend} {final.model or '-'} {'ok' if final.ok else 'FAILED'} "
             f"{final.duration}s in={final.tokens_in} out={final.tokens_out}{cr}")
        if not final.ok:
            _err(final.error or "no answer")
    return EXIT_OK if final.ok else EXIT_FAIL


def cmd_stats(args: argparse.Namespace, cfg: dict) -> int:
    since = time.time() - args.days * 86400 if args.days else None
    summary = runlog.summarize(runlog.read(log_path(), since))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(runlog.format_table(summary))
    return EXIT_OK


def cmd_check(args: argparse.Namespace, cfg: dict) -> int:
    paths = [Path(p) for p in (args.paths or ["."])]
    try:
        for p in paths:
            if p.is_dir():
                guard.check(p, [], cfg["guard"])
            else:
                guard.check(p.parent, [p], cfg["guard"])
    except guard.GuardError as e:
        print(f"refused: {e}")
        return EXIT_GUARD
    print("ok: delegation allowed")
    return EXIT_OK


def cmd_config(args: argparse.Namespace, cfg: dict) -> int:
    print(f"config: {config_path()}{'' if config_path().exists() else ' (not found, using defaults)'}")
    print(f"log:    {log_path()}")
    for name, t in cfg["tasks"].items():
        fb = f" -> {t['fallback']}" if t.get("fallback") else ""
        print(f"  {name:<10} {t['backend']}{fb}  mode={t.get('mode')} model={t.get('model') or 'default'}")
    print(f"  deny_paths: {len(cfg['guard'].get('deny_paths', []))}")
    return EXIT_OK


def build_parser(tasks: list[str]) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="delegate", description="Delegate work to agy / Copilot CLI with guardrails.")
    p.add_argument("--version", action="version", version=f"delegate {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    for t in tasks:
        sp = sub.add_parser(t, help=f"run the '{t}' task")
        sp.add_argument("prompt", nargs="?", help="prompt text, or '-' to read stdin")
        sp.add_argument("-f", "--file", dest="files", action="append", help="file to include (repeatable)")
        sp.add_argument("-C", default=".", help="working directory (default: .)")
        sp.add_argument("--add-dir", action="append", help="extra directory the backend may read")
        sp.add_argument("--model", default=None, help="override model")
        sp.add_argument("--backend", choices=["agy", "copilot"], help="force a backend (disables fallback)")
        sp.add_argument("--no-fallback", action="store_true")
        sp.add_argument("--timeout", type=int, help="seconds")
        sp.add_argument("--json", action="store_true", help="print the full result as JSON")
        sp.set_defaults(func=cmd_run, task=t)

    st = sub.add_parser("stats", help="cost and success summary from the log")
    st.add_argument("--days", type=float, default=0, help="only the last N days")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_stats)

    ck = sub.add_parser("check", help="test whether a path may be delegated")
    ck.add_argument("paths", nargs="*")
    ck.set_defaults(func=cmd_check)

    cf = sub.add_parser("config", help="show effective configuration")
    cf.set_defaults(func=cmd_config)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = load()
    except ConfigError as e:
        _err(f"config error: {e}")
        return EXIT_USAGE
    args = build_parser(list(cfg["tasks"])).parse_args(argv)
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
