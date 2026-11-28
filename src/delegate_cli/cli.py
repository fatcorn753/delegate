"""delegate: 作業を agy / Copilot CLI に安全に委任する薄いラッパー。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import __version__, backends, guard, implement, runlog
from .config import EDIT, GITHUB, IMPLEMENT, ConfigError, config_path, load, log_path

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


def _github_slug(cwd: Path) -> str:
    """origin が GitHub なら owner/name を返す。"""
    url = _git(cwd, "remote", "get-url", "origin").stdout.strip()
    for prefix in ("git@github.com:", "https://github.com/", "ssh://git@github.com/"):
        if url.startswith(prefix):
            slug = url[len(prefix):]
            return slug[:-4] if slug.endswith(".git") else slug
    return ""


def _read_prompt(args: argparse.Namespace) -> str:
    prompt = args.prompt
    # 標準入力は '-' を明示したときだけ読む。呼び出し元によっては stdin が開いたままで、
    # 暗黙に読むと止まってしまうため。
    if prompt == "-":
        prompt = sys.stdin.read()
    return (prompt or "").strip()


def _file_listing(cwd: Path, guard_cfg: dict, limit: int = 400) -> str:
    """作業ディレクトリのファイル一覧(相対パス)。

    読み取り専用の agy はシェルを使えず、ファイル一覧を取る手段がないため、README のない
    プロジェクトでは調べ始められない(2026-11 に確認)。delegate 側で一覧を渡す。
    """
    skip = set(guard_cfg.get("skip_dirs", []))
    paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(cwd):
        dirnames[:] = sorted(d for d in dirnames if d not in skip and not d.startswith("."))
        for f in sorted(filenames):
            if f == ".DS_Store":
                continue
            paths.append(str((Path(dirpath) / f).relative_to(cwd)))
            if len(paths) >= limit:
                return "\n".join(paths) + f"\n... (truncated at {limit} files)"
    return "\n".join(paths)


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
    if mode == IMPLEMENT and not args.test:
        _err("implement needs --test '<command>' (the command that decides success)")
        return EXIT_USAGE
    # implement の -f は仕様書で、本文をプロンプトに埋め込むので委任先に読ませる必要はない
    add_dirs = [] if mode == IMPLEMENT else sorted({f.parent for f in files if not str(f).startswith(str(cwd) + os.sep)})
    add_dirs += [Path(os.path.realpath(Path(d).expanduser())) for d in (args.add_dir or [])]

    try:
        # gh タスクの委任先はローカルのファイルを一切読めないので、秘匿ファイルを調べる必要はない
        scan = guard.Scan() if mode == GITHUB else guard.scan_all(cwd, [*files, *add_dirs], cfg["guard"])
        if mode == GITHUB:
            guard.check_paths([cwd], cfg["guard"])
        before = _edit_precheck(cwd) if mode in (EDIT, IMPLEMENT) else set()
    except guard.GuardError as e:
        _err(f"refused: {e}")
        return EXIT_GUARD

    if mode == IMPLEMENT:
        return cmd_implement(args, cfg, task, cwd, files, prompt, scan, before)

    full_prompt = _compose(task.get("preamble", ""), prompt, files)
    if task.get("list_files") and mode != GITHUB:
        full_prompt += f"\n\nFiles in the working directory ({cwd}):\n{_file_listing(cwd, cfg['guard'])}"
    run_dir = cwd
    if mode == GITHUB:
        slug = _github_slug(cwd)
        if slug:
            full_prompt = f"Repository: {slug}\n\n{full_prompt}"
        # 委任先が作業ディレクトリのファイルに触れないよう、空のディレクトリで動かす
        run_dir = Path(tempfile.mkdtemp(prefix="delegate-gh-"))
    chain = [args.backend or task["backend"]]
    if not args.backend and not args.no_fallback and task.get("fallback"):
        chain.append(task["fallback"])

    results = []
    refused = []
    try:
        for i, name in enumerate(chain):
            # --model は最初の委任先にだけ効かせる。モデル名は委任先ごとに違うので、
            # フォールバック先へ渡すと両方失敗するため(フォールバック先は既定モデル)。
            if i == 0:
                model = args.model if args.model is not None else (task.get("model", "") if name == task["backend"] else "")
            else:
                model = ""
            try:
                masked = guard.decide(name, scan, cfg["guard"])
            except guard.GuardError as e:
                r = backends.Result(backend=name, ok=False, error=f"refused: {e}", model=model)
                refused.append(name)
            else:
                if masked:
                    _err(f"{name}: hiding {len(masked)} secret-like file(s) from the backend")
                try:
                    r = backends.run(name, cfg["backends"][name], full_prompt, run_dir, model, mode,
                                     add_dirs, args.timeout, masked, protect=scan.secrets,
                                     commands=task.get("agy_commands"))
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
                "secrets": len(scan.secrets),
                "error": r.error[:300],
            })
            if r.ok:
                break
            if mode == EDIT and i + 1 < len(chain) and _edit_report(cwd, before) != "no file changes":
                # 途中まで書き換えた上に別の委任先が重ねて編集すると、どちらの変更か分からなくなる
                _err(f"{name} failed after changing files; not falling back (review or revert the changes)")
                break
            if i + 1 < len(chain):
                first = r.error.strip().splitlines()[0] if r.error.strip() else "no answer"
                _err(f"{name} failed ({first}); falling back to {chain[i + 1]}")
    finally:
        if run_dir != cwd:
            shutil.rmtree(run_dir, ignore_errors=True)

    if len(refused) == len(chain):
        for r in results:
            _err(r.error)
        return EXIT_GUARD

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


def cmd_implement(args: argparse.Namespace, cfg: dict, task: dict, cwd: Path, files: list[Path], prompt: str,
                  scan: guard.Scan, before: set[str]) -> int:
    name = args.backend or task["backend"]
    try:
        masked = guard.decide(name, scan, cfg["guard"])
    except guard.GuardError as e:
        _err(f"refused: {e}")
        return EXIT_GUARD
    if masked:
        _err(f"{name}: hiding {len(masked)} secret-like file(s) from the backend")
    spec_parts = [f"--- {f.name} ---\n{f.read_text(errors='replace')}" for f in files]
    spec = "\n\n".join([*spec_parts, prompt] if prompt else spec_parts)
    head = _git(cwd, "rev-parse", "HEAD").stdout.strip()
    model = args.model if args.model is not None else task.get("model", "")
    rounds = args.rounds or int(task.get("rounds", 3))
    test_timeout = args.test_timeout or int(task.get("test_timeout", 600))

    def log_round(rd: implement.Round) -> None:
        runlog.append(log_path(), {
            "ts": runlog.now(), "task": args.task, "backend": name, "model": model, "cwd": str(cwd),
            "ok": rd.ok, "duration": rd.duration, "tokens_in": rd.tokens_in, "tokens_out": rd.tokens_out,
            "credits": rd.credits, "fallback_of": None, "prompt_chars": len(spec), "secrets": len(scan.secrets),
            "round": rd.round, "test_passed": rd.test_passed, "error": rd.error,
        })
        state = "error" if not rd.ok else ("tests pass" if rd.test_passed else "tests FAIL")
        _err(f"round {rd.round}: {state} ({rd.duration}s + test {rd.test_duration}s)")

    out = implement.implement(
        bcfg=cfg["backends"][name], backend=name, preamble=task.get("preamble", ""), spec=spec,
        test_cmd=args.test, cwd=cwd, model=model, masked=masked,
        listing=_file_listing(cwd, cfg["guard"]), rounds=rounds, timeout=args.timeout,
        test_timeout=test_timeout, diff_stat=lambda: _git(cwd, "diff", "--stat").stdout.rstrip(),
        on_round=log_round)
    report = _edit_report(cwd, before)
    if _git(cwd, "rev-parse", "HEAD").stdout.strip() != head:
        # 作業役にはシェルを与えていないので起きないはずだが、起きたら成功扱いにしない
        out.passed = False
        out.error = (out.error + "; " if out.error else "") + "HEAD moved: the worker committed"

    if args.json:
        print(json.dumps(out.to_dict() | {"task": args.task, "backend": name, "test_cmd": args.test,
                                          "edit_report": report}, ensure_ascii=False, indent=2))
    else:
        if out.worker_text:
            print(out.worker_text)
        print("\n--- changes ---\n" + report)
        verdict = "PASS" if out.passed else "FAIL"
        print(f"\n--- tests: {verdict} after {len(out.rounds)} round(s): {args.test} ---")
        if not out.passed and out.test_tail:
            print(out.test_tail[-1500:])
        cr = "" if out.credits is None else f" credits={out.credits:.3f}"
        _err(f"{name} {out.model or '-'} {verdict} rounds={len(out.rounds)} {out.duration}s "
             f"in={out.tokens_in} out={out.tokens_out}{cr}")
        if out.error:
            _err(out.error)
    return EXIT_OK if out.passed else EXIT_FAIL


def cmd_stats(args: argparse.Namespace, cfg: dict) -> int:
    since = time.time() - args.days * 86400 if args.days else None
    summary = runlog.summarize(runlog.read(log_path(), since))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(runlog.format_table(summary))
    return EXIT_OK


def cmd_check(args: argparse.Namespace, cfg: dict) -> int:
    code = EXIT_OK
    for raw in args.paths or ["."]:
        p = Path(raw)
        try:
            scan = guard.scan_all(p, [], cfg["guard"]) if p.is_dir() else guard.scan_all(p.parent, [p], cfg["guard"])
        except guard.GuardError as e:
            print(f"{raw}: refused: {e}")
            code = EXIT_GUARD
            continue
        state = "" if scan.complete else " (scan hit its limit)"
        print(f"{raw}: {len(scan.secrets)} secret-like file(s){state}")
        for s in scan.secrets[:10]:
            print(f"  {s}")
        for name in cfg["backends"]:
            try:
                masked = guard.decide(name, scan, cfg["guard"])
                verdict = f"ok, hides {len(masked)} file(s)" if masked else "ok"
            except guard.GuardError:
                verdict = "refused"
            print(f"  {name}: {verdict} (secret_policy={guard.policy_for(name, cfg['guard'])})")
    return code


def cmd_config(args: argparse.Namespace, cfg: dict) -> int:
    print(f"config: {config_path()}{'' if config_path().exists() else ' (not found, using defaults)'}")
    print(f"log:    {log_path()}")
    for name, t in cfg["tasks"].items():
        fb = f" -> {t['fallback']}" if t.get("fallback") else ""
        print(f"  {name:<10} {t['backend']}{fb}  mode={t.get('mode')} model={t.get('model') or 'default'}")
    print(f"  deny_paths: {len(cfg['guard'].get('deny_paths', []))}")
    print(f"  secret_policy: {cfg['guard'].get('secret_policy')}")
    return EXIT_OK


def build_parser(tasks: dict[str, dict]) -> argparse.ArgumentParser:
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
        sp.add_argument("--timeout", type=int, help="seconds for the backend CLI (not the pre-run secret scan)")
        sp.add_argument("--json", action="store_true", help="print the full result as JSON")
        if tasks[t].get("mode") == IMPLEMENT:
            sp.add_argument("--test", help="test command run after each round; exit 0 means done (required)")
            sp.add_argument("--rounds", type=int, help="max worker rounds including the first (default from config)")
            sp.add_argument("--test-timeout", type=int, help="seconds per test run")
        sp.set_defaults(func=cmd_run, task=t, test=None, rounds=None, test_timeout=None)

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
    args = build_parser(cfg["tasks"]).parse_args(argv)
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
