"""Deterministic checkpoints (C6): the SAME code runs in live processing and in replay.

A ``Checkpointer`` observes the engine after every raw event (live: as a pipeline consumer on the
dispatch thread; replay: from the replay loop) and records ``(seq, raw_count, kind, state_hash)``
when a deterministic trigger fires:

* ``every_n``   : raw ``seq % every_n == 0``
* ``bar_close`` : the number of completed 30 s bars changed
* ``health``    : connection / farm / not-live / 10197 / alerts / contract / book state / stream
                  generation-status-error changed (``fingerprint.health_token``)
* ``final``     : explicit, at the end of processing

Triggers depend only on the event stream, so live and replay of the same raw stream through the
same code produce the same checkpoint sequence. Live checkpoints are written once, at shutdown,
to ``<session_dir>/checkpoints.json`` (never I/O on the dispatch thread).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from hermes.replay import fingerprint as fp

SIDECAR_NAME = "checkpoints.json"
SIDECAR_FORMAT = "hermes-checkpoints"
SIDECAR_VERSION = 1


@dataclass(frozen=True, slots=True)
class CheckpointPolicy:
    every_n: int = 10_000            # 0 disables
    on_bar_close: bool = True
    on_health: bool = True
    keep_summary: bool = False       # store a compact human-readable summary with each checkpoint
    max_checkpoints: int = 200_000   # bounded memory; overflow is counted (and breaks equivalence claims)


@dataclass(frozen=True, slots=True)
class Checkpoint:
    seq: int
    raw_count: int
    kind: str                        # trigger names joined with "+", e.g. "bar_close+health"
    hash: str
    summary: dict | None = None

    def key(self) -> tuple[int, str]:
        return (self.seq, self.kind)


class Checkpointer:
    def __init__(self, engine, policy: CheckpointPolicy | None = None, timer=None) -> None:
        self.engine = engine
        self._timer = timer                  # optional: measures hashing cost only (never engine time)
        self.hash_ns = 0
        self.policy = policy or CheckpointPolicy()
        self.checkpoints: list[Checkpoint] = []
        self.final: Checkpoint | None = None
        self.raw_count = 0
        self.last_seq = 0
        self.dropped = 0
        self.hash_calls = 0
        self._bars = -1
        self._health: tuple | None = None

    # live pipeline consumer interface
    def after_event(self, raw, events, now_mono_ns: int) -> None:
        self.after_raw(raw.seq)

    def after_raw(self, seq: int) -> None:
        self.raw_count += 1
        self.last_seq = seq
        p = self.policy
        kind = ""
        if p.every_n and seq % p.every_n == 0:
            kind = "every_n"
        if p.on_bar_close:
            n = fp.bar_count(self.engine)
            if n != self._bars:
                if self._bars >= 0:
                    kind = kind + "+bar_close" if kind else "bar_close"
                self._bars = n
        if p.on_health:
            tok = fp.health_token(self.engine)
            if tok != self._health:
                kind = kind + "+health" if kind else "health"
                self._health = tok
        if kind:
            self._record(kind)

    def _make(self, kind: str) -> Checkpoint:
        self.hash_calls += 1
        t = self._timer() if self._timer is not None else 0
        summary = fp.compact_summary(self.engine) if self.policy.keep_summary else None
        cp = Checkpoint(self.last_seq, self.raw_count, kind, fp.state_hash(self.engine), summary)
        if self._timer is not None:
            self.hash_ns += self._timer() - t
        return cp

    def _record(self, kind: str) -> None:
        if len(self.checkpoints) >= self.policy.max_checkpoints:
            self.dropped += 1
            return
        self.checkpoints.append(self._make(kind))

    def finalize(self) -> Checkpoint:
        self.final = self._make("final")
        return self.final

    # ------------------------------------------------------------------ persistence
    def to_dict(self, **meta: Any) -> dict:
        return {
            "format": SIDECAR_FORMAT, "version": SIDECAR_VERSION, "hash_version": fp.HASH_VERSION,
            "policy": asdict(self.policy), "raw_events": self.raw_count, "last_seq": self.last_seq,
            "dropped": self.dropped, **meta,
            "checkpoints": [[c.seq, c.raw_count, c.kind, c.hash] for c in self.checkpoints],
            "final": None if self.final is None else [self.final.seq, self.final.raw_count, "final", self.final.hash],
        }

    def save(self, path: Path, **meta: Any) -> Path:
        path = Path(path)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(**meta), separators=(",", ":")))
        tmp.replace(path)
        return path


@dataclass(slots=True)
class CheckpointSet:
    """Checkpoints loaded from a sidecar / saved replay result."""
    meta: dict
    policy: CheckpointPolicy
    checkpoints: list[Checkpoint]
    final: Checkpoint | None


def load_checkpoints(path: Path) -> CheckpointSet:
    d = json.loads(Path(path).read_text())
    if d.get("format") != SIDECAR_FORMAT or d.get("version") != SIDECAR_VERSION:
        raise ValueError(f"{path}: not a Hermès checkpoint file (format/version)")
    pol = CheckpointPolicy(**d["policy"])
    cps = [Checkpoint(s, n, k, h) for s, n, k, h in d["checkpoints"]]
    fin = None if d.get("final") is None else Checkpoint(d["final"][0], d["final"][1], "final", d["final"][3])
    meta = {k: v for k, v in d.items() if k not in ("checkpoints", "final", "policy")}
    return CheckpointSet(meta, pol, cps, fin)


@dataclass(slots=True)
class CompareResult:
    compared: int = 0
    matched: int = 0
    only_a: int = 0
    only_b: int = 0
    first_mismatch: tuple | None = None       # (seq, kind, hash_a, hash_b)
    final_compared: bool = False
    final_match: bool = False
    limited_to_seq: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def equivalent(self) -> bool:
        return (self.compared > 0 and self.matched == self.compared and not self.only_a and not self.only_b
                and (self.final_match or not self.final_compared))


def compare_checkpoints(a: list[Checkpoint], b: list[Checkpoint], final_a: Checkpoint | None = None,
                        final_b: Checkpoint | None = None, limit_seq: int | None = None) -> CompareResult:
    """Pairwise comparison by (seq, kind). ``limit_seq`` restricts the claim to the verified prefix."""
    r = CompareResult(limited_to_seq=limit_seq)
    da = {c.key(): c for c in a if limit_seq is None or c.seq <= limit_seq}
    db = {c.key(): c for c in b if limit_seq is None or c.seq <= limit_seq}
    for key in sorted(set(da) | set(db)):
        ca, cb = da.get(key), db.get(key)
        if ca is None:
            r.only_b += 1
        elif cb is None:
            r.only_a += 1
        else:
            r.compared += 1
            if ca.hash == cb.hash:
                r.matched += 1
                continue
        if r.first_mismatch is None:
            r.first_mismatch = (key[0], key[1], ca.hash if ca else None, cb.hash if cb else None)
    if final_a is not None and final_b is not None and (limit_seq is None or max(final_a.seq, final_b.seq) <= limit_seq):
        r.final_compared = True
        r.final_match = final_a.seq == final_b.seq and final_a.hash == final_b.hash
        if not r.final_match and r.first_mismatch is None:
            r.first_mismatch = (final_a.seq, "final", final_a.hash, final_b.hash)
    return r
