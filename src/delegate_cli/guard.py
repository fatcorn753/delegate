"""委任前の安全確認。拒否パスでは実行させず、秘匿ファイルは委任先ごとの方針で扱う。"""

from __future__ import annotations

import fnmatch
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

ALLOW, MASK, REFUSE = "allow", "mask", "refuse"

# 中身で判定する秘匿情報のパターン。ファイル名では見分けられない退避ファイルや設定ファイル向け。
SECRET_PATTERNS = [
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"\b[MNO][A-Za-z0-9_-]{23,27}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,40}\b"),  # Discord bot token
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),  # GitHub token
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(rb"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b"),  # OpenAI / Anthropic
    re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API key
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key
    re.compile(rb"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),  # Slack
    re.compile(rb"https?://[^/\s:@]+:[^/\s@]{8,}@"),  # URL に埋め込まれた認証情報(.git/config など)
]


class GuardError(Exception):
    pass


@dataclass
class Scan:
    """作業ディレクトリの秘匿ファイル検出結果。complete が False なら上限で打ち切った。"""

    secrets: list[Path] = field(default_factory=list)
    complete: bool = True


def _real(p: str | Path) -> Path:
    # macOS では同じ日本語名が NFC と NFD の両方で現れるため、比較前に NFC へそろえる
    return Path(unicodedata.normalize("NFC", os.path.realpath(Path(p).expanduser())))


def _key(p: Path) -> Path:
    # macOS の既定ファイルシステムは大文字小文字を区別しないので、~/Artistme/AI-OS のような
    # 表記違いで拒否をすり抜けないよう比較時だけ casefold する
    return Path(str(p).casefold()) if sys.platform == "darwin" else p


def _is_under(path: Path, root: Path) -> bool:
    path, root = _key(path), _key(root)
    return path == root or root in path.parents


def check_paths(paths: list[Path], guard: dict) -> None:
    """paths のどれかが deny_paths 配下なら GuardError。"""
    roots = [_real(d) for d in guard.get("deny_paths", [])]
    for p in paths:
        rp = _real(p)
        for root in roots:
            if _is_under(rp, root):
                raise GuardError(f"{rp} is inside a denied path ({root}); not delegating")


def _name_is_secret(name: str, guard: dict) -> bool:
    if any(fnmatch.fnmatch(name, g) for g in guard.get("secret_ignore_globs", [])):
        return False
    return any(fnmatch.fnmatch(name, g) for g in guard.get("secret_globs", []))


def _content_is_secret(path: Path, guard: dict) -> bool:
    if not guard.get("scan_content", True):
        return False
    try:
        if path.stat().st_size > int(guard.get("max_file_bytes", 262144)):
            return False
        data = path.read_bytes()
    except OSError:
        return False
    if b"\0" in data[:4096]:
        return False  # バイナリは見ない
    return any(p.search(data) for p in SECRET_PATTERNS)


def is_secret(path: Path, guard: dict) -> bool:
    return _name_is_secret(path.name, guard) or _content_is_secret(path, guard)


def find_secrets(root: Path, guard: dict) -> Scan:
    """root 以下の秘匿ファイルを、名前と中身の両方で探す。"""
    root = _real(root)
    depth_limit = int(guard.get("scan_depth", 12))
    max_files = int(guard.get("max_scan_files", 20000))
    skip = set(guard.get("skip_dirs", []))
    scan = Scan()
    seen = 0
    base_depth = len(root.parts)
    # .git は丸ごと飛ばすが、リモート URL にトークンが入りうる .git/config だけは見る
    git_config = root / ".git" / "config"
    if git_config.is_file() and _content_is_secret(git_config, guard):
        scan.secrets.append(git_config)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - base_depth
        dirnames[:] = [d for d in dirnames if d not in skip]
        if depth >= depth_limit and dirnames:
            scan.complete = False
            dirnames[:] = []
        for f in filenames:
            seen += 1
            if seen > max_files:
                scan.complete = False
                return scan
            p = Path(dirpath) / f
            if is_secret(p, guard):
                scan.secrets.append(p)
    return scan


def scan_all(workdir: Path, extra_paths: list[Path], guard: dict) -> Scan:
    """拒否パスを確認したうえで、作業ディレクトリと追加パスの秘匿ファイルをまとめて返す。"""
    check_paths([workdir, *extra_paths], guard)
    total = Scan()
    for d in [workdir, *[p for p in extra_paths if Path(p).is_dir()]]:
        s = find_secrets(d, guard)
        total.secrets += s.secrets
        total.complete = total.complete and s.complete
    for p in extra_paths:
        if Path(p).is_file() and is_secret(Path(p), guard):
            total.secrets.append(_real(p))
    total.secrets = sorted(set(total.secrets))
    return total


def policy_for(backend: str, guard: dict) -> str:
    pol = (guard.get("secret_policy") or {}).get(backend, MASK)
    if pol not in (ALLOW, MASK, REFUSE):
        raise GuardError(f"guard.secret_policy.{backend} must be allow, mask or refuse")
    return pol


def decide(backend: str, scan: Scan, guard: dict) -> list[Path]:
    """委任先に隠すべきファイルを返す。委任できないなら GuardError。"""
    if not scan.secrets and scan.complete:
        return []
    pol = policy_for(backend, guard)
    if pol == ALLOW:
        return []
    listed = "\n  ".join(str(p) for p in scan.secrets[:10])
    if pol == REFUSE:
        raise GuardError(f"secret-like files found and secret_policy.{backend}=refuse:\n  {listed}")
    if not scan.complete:
        # 全部を調べ切れていないと、隠し漏れが出うる
        raise GuardError(
            f"secret scan stopped at its limit (scan_depth/max_scan_files), so {backend} cannot be "
            "masked safely; narrow -C or raise the limits"
        )
    return scan.secrets
