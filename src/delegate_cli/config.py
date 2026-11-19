"""設定の読み込み。組み込みの既定値に、ユーザーの config.toml を重ねる。"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

READ = "read"
EDIT = "edit"

DEFAULTS: dict[str, Any] = {
    "guard": {
        # この配下では一切実行しない(秘匿情報を含むリポジトリなど)
        "deny_paths": [],
        # 作業ディレクトリ内にこれらがあれば実行を拒否する
        "secret_globs": [
            ".env",
            ".env.*",
            "*.pem",
            "*.key",
            "*.p12",
            "id_rsa*",
            "id_ed25519*",
            "credentials*.json",
            "service-account*.json",
            "*.keystore",
            "*.jks",
        ],
        # .env.example のような見本ファイルは対象外にする
        "secret_ignore_globs": ["*.example", "*.sample", "*.template", "*.dist"],
        "scan_depth": 3,
        "skip_dirs": [".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".gradle"],
    },
    "backends": {
        "agy": {"command": "agy", "timeout": 600},
        "copilot": {"command": "copilot", "timeout": 600},
    },
    "tasks": {
        "survey": {
            "backend": "agy",
            "fallback": "copilot",
            "mode": READ,
            "model": "",
            "preamble": "Read-only investigation. Do not modify any file. "
            "Answer with the conclusion first, concisely, citing file paths. "
            "Reply in the language of the request.",
        },
        "summarize": {
            "backend": "agy",
            "fallback": "copilot",
            "mode": READ,
            "model": "gemini-3.8-flash-low",
            "preamble": "Read-only. Summarize the given files. Do not modify any file. "
            "Reply in the language of the request.",
        },
        "ask": {
            "backend": "agy",
            "fallback": "copilot",
            "mode": READ,
            "model": "",
            "preamble": "Answer the question concisely. Do not modify any file. "
            "Reply in the language of the request.",
        },
        "gh": {
            "backend": "copilot",
            "fallback": "",
            "mode": READ,
            "model": "",
            "preamble": "Read-only GitHub task. Never create, edit, close, merge or comment on anything. "
            "Reply in the language of the request.",
        },
        "edit": {
            "backend": "copilot",
            "fallback": "",
            "mode": EDIT,
            "model": "",
            "preamble": "Make only the requested change, nothing else. Do not commit, push or run "
            "destructive commands. Reply in the language of the request with a short summary.",
        },
    },
}


def config_path() -> Path:
    env = os.environ.get("DELEGATE_CONFIG")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "delegate" / "config.toml"


def log_path() -> Path:
    env = os.environ.get("DELEGATE_LOG")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(base).expanduser() / "delegate" / "log.jsonl"


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class ConfigError(Exception):
    pass


def load(path: Path | None = None) -> dict[str, Any]:
    path = path or config_path()
    cfg = copy.deepcopy(DEFAULTS)
    if path.exists():
        try:
            with path.open("rb") as f:
                cfg = _merge(cfg, tomllib.load(f))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from e
    _validate(cfg)
    return cfg


def _validate(cfg: dict[str, Any]) -> None:
    backends = cfg["backends"]
    for name, t in cfg["tasks"].items():
        if not t.get("backend"):
            raise ConfigError(f"tasks.{name}.backend is required")
        for key in ("backend", "fallback"):
            b = t.get(key, "")
            if b and b not in backends:
                raise ConfigError(f"tasks.{name}.{key}: unknown backend {b!r}")
        if t.get("mode", READ) not in (READ, EDIT):
            raise ConfigError(f"tasks.{name}.mode must be 'read' or 'edit'")
