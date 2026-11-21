"""implement タスク: 作業役に仕様を実装させ、delegate がテストを回して失敗を差し戻す。

作業役(copilot)にはシェルを与えない。テストは delegate が親の指定したコマンドで回す。
作業役がシェルを持つと、秘匿ファイルを隠す仕組み(read の deny)を cat などで抜けられるため。
"""

from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import backends, guard

TEST_TAIL_CHARS = 4000


@dataclass
class Round:
    round: int
    ok: bool
    duration: float
    tokens_in: int
    tokens_out: int
    credits: float | None
    test_passed: bool | None = None
    test_duration: float = 0.0
    error: str = ""


@dataclass
class Outcome:
    passed: bool
    rounds: list[Round] = field(default_factory=list)
    worker_text: str = ""
    test_tail: str = ""
    error: str = ""
    model: str = ""

    @property
    def tokens_in(self) -> int:
        return sum(r.tokens_in for r in self.rounds)

    @property
    def tokens_out(self) -> int:
        return sum(r.tokens_out for r in self.rounds)

    @property
    def credits(self) -> float | None:
        vals = [r.credits for r in self.rounds if r.credits is not None]
        return round(sum(vals), 4) if vals else None

    @property
    def duration(self) -> float:
        return round(sum(r.duration + r.test_duration for r in self.rounds), 2)

    def to_dict(self) -> dict:
        d = asdict(self)
        d |= {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
              "credits": self.credits, "duration": self.duration}
        return d


def redact(text: str) -> str:
    """テスト出力に混じったトークンや秘密鍵の形を伏せる。作業役へ差し戻す前に通す。"""
    data = text.encode()
    for p in guard.SECRET_PATTERNS:
        data = p.sub(b"[REDACTED]", data)
    return data.decode(errors="replace")


def run_tests(cmd: str, cwd: Path, timeout: int) -> tuple[bool, str, float]:
    t0 = time.monotonic()
    try:
        p = subprocess.run(["sh", "-c", cmd], cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        passed, out = p.returncode == 0, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        passed, out = False, out + f"\n[delegate] test command timed out after {timeout}s"
    return passed, redact(out)[-TEST_TAIL_CHARS:], round(time.monotonic() - t0, 2)


def first_prompt(preamble: str, spec: str, test_cmd: str, listing: str) -> str:
    parts = [preamble, f"Specification:\n{spec}", f"The caller will run this test command after you finish: {test_cmd}"]
    if listing:
        parts.append(f"Files in the repository:\n{listing}")
    return "\n\n".join(p for p in parts if p)


def retry_prompt(test_cmd: str, tail: str, stat: str) -> str:
    return (f"The test command `{test_cmd}` failed after your change. Fix the implementation so the tests "
            f"pass, still changing only what the specification requires.\n\n"
            f"Current changes (git diff --stat):\n{stat or 'none'}\n\nTest output (tail):\n{tail}")


def _delta(total, seen):
    # 累計が前回より小さければ、累計ではなくそのラウンド単独の値とみなす
    return total - seen if total >= seen else total


def implement(*, bcfg: dict, backend: str, preamble: str, spec: str, test_cmd: str, cwd: Path, model: str,
              masked: list[Path], listing: str, rounds: int, timeout: int | None, test_timeout: int,
              diff_stat, on_round=None) -> Outcome:
    """作業役の実装 → テスト → 失敗なら差し戻し、を最大 rounds 回くり返す。"""
    outcome = Outcome(passed=False)
    session = str(uuid.uuid4())
    # copilot は同じ session の累計を返す(2026-11-21 に確認)ので、前回までとの差分をそのラウンドの値にする
    seen_in = seen_out = 0
    seen_cr = 0.0
    prompt = first_prompt(preamble, spec, test_cmd, listing)
    for k in range(1, max(rounds, 1) + 1):
        try:
            r = backends.run(backend, bcfg, prompt, cwd, model, "implement", [], timeout, masked, session)
        except backends.BackendError as e:
            r = backends.Result(backend=backend, ok=False, error=str(e), model=model)
        d_in, d_out = _delta(r.tokens_in, seen_in), _delta(r.tokens_out, seen_out)
        d_cr = None if r.credits is None else round(_delta(r.credits, seen_cr), 6)
        seen_in, seen_out = max(seen_in, r.tokens_in), max(seen_out, r.tokens_out)
        seen_cr = max(seen_cr, r.credits or 0.0)
        rd = Round(round=k, ok=r.ok, duration=r.duration, tokens_in=d_in,
                   tokens_out=d_out, credits=d_cr, error=r.error[:300])
        outcome.rounds.append(rd)
        outcome.model = r.model or outcome.model
        if r.text:
            outcome.worker_text = r.text
        if not r.ok:
            outcome.error = r.error or "no answer"
            if on_round:
                on_round(rd)
            break
        passed, tail, tdur = run_tests(test_cmd, cwd, test_timeout)
        rd.test_passed, rd.test_duration = passed, tdur
        outcome.test_tail = tail
        if on_round:
            on_round(rd)
        if passed:
            outcome.passed = True
            break
        prompt = retry_prompt(test_cmd, tail, diff_stat())
    return outcome
