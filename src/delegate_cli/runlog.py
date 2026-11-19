"""呼び出しログ(JSONL)の追記と集計。"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path


def append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read(path: Path, since: float | None = None) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # 壊れた行は集計から外す
        if since is None or rec.get("ts", 0) >= since:
            rows.append(rec)
    return rows


def summarize(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r.get("backend", "?"), r.get("task", "?"))].append(r)
    out = []
    for (backend, task), rs in sorted(groups.items()):
        ok = sum(1 for r in rs if r.get("ok"))
        credits = [r["credits"] for r in rs if r.get("credits") is not None]
        out.append({
            "backend": backend,
            "task": task,
            "calls": len(rs),
            "ok": ok,
            "avg_sec": round(sum(r.get("duration", 0) for r in rs) / len(rs), 1),
            "tokens_in": sum(r.get("tokens_in", 0) for r in rs),
            "tokens_out": sum(r.get("tokens_out", 0) for r in rs),
            "credits": round(sum(credits), 3) if credits else None,
        })
    return out


def format_table(summary: list[dict]) -> str:
    if not summary:
        return "no calls recorded"
    head = f"{'backend':<8} {'task':<10} {'calls':>5} {'ok':>4} {'avg_s':>6} {'tok_in':>9} {'tok_out':>8} {'credits':>8}"
    lines = [head]
    for s in summary:
        cr = "-" if s["credits"] is None else f"{s['credits']:.3f}"
        lines.append(
            f"{s['backend']:<8} {s['task']:<10} {s['calls']:>5} {s['ok']:>4} {s['avg_sec']:>6} "
            f"{s['tokens_in']:>9} {s['tokens_out']:>8} {cr:>8}"
        )
    return "\n".join(lines)


def now() -> float:
    return time.time()
