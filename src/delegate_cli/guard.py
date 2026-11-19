"""委任前の安全確認。拒否パスと秘匿ファイルを検出したら実行させない。"""

from __future__ import annotations

import fnmatch
import os
import sys
import unicodedata
from pathlib import Path


class GuardError(Exception):
    pass


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


def _is_secret(name: str, guard: dict) -> bool:
    if any(fnmatch.fnmatch(name, g) for g in guard.get("secret_ignore_globs", [])):
        return False
    return any(fnmatch.fnmatch(name, g) for g in guard.get("secret_globs", []))


def find_secrets(root: Path, guard: dict) -> list[Path]:
    """root 以下 scan_depth 階層までの秘匿ファイルらしきものを返す。"""
    root = _real(root)
    depth_limit = int(guard.get("scan_depth", 3))
    skip = set(guard.get("skip_dirs", []))
    hits: list[Path] = []
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - base_depth
        dirnames[:] = [d for d in dirnames if d not in skip] if depth < depth_limit else []
        for f in filenames:
            if _is_secret(f, guard):
                hits.append(Path(dirpath) / f)
    return hits


def check(workdir: Path, extra_paths: list[Path], guard: dict) -> None:
    """作業ディレクトリと追加パスをまとめて検査する。"""
    check_paths([workdir, *extra_paths], guard)
    found: list[Path] = []
    for d in [workdir, *[p for p in extra_paths if Path(p).is_dir()]]:
        found += find_secrets(d, guard)
    for p in extra_paths:
        if Path(p).is_file() and _is_secret(Path(p).name, guard):
            found.append(_real(p))
    if found:
        listed = "\n  ".join(str(p) for p in sorted(set(found))[:10])
        raise GuardError(
            "secret-like files found; not delegating (adjust guard.secret_globs if these are safe):\n  "
            + listed
        )
