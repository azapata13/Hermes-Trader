"""C9f decision journal / decision checkpoint persistence and live-vs-replay comparison.

Kept SEPARATE from the market checkpoints (``checkpoints.json`` / ``state_hash``): a live run writes
``<session_dir>/decisions.json`` once at shutdown (never I/O on the dispatch thread); replay rebuilds
the same journal with the same ``DecisionRuntime`` and compares:

* every journal record (kind, seq, event time, instrument, setup_id, proposal_id, approval_view_id,
  status, direction, approval_allowed_now, reason codes, decision fingerprint);
* every decision checkpoint (seq, kinds, fingerprint) and the final decision fingerprint.

An equivalence claim requires the same decision code (``decision_code_fingerprint``), the same
decision config and the same market code/config (the decision layer reads market state).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hermes.decision.runtime import JOURNAL_SCHEMA_VERSION, DecisionCheckpoint, DecisionRuntime, JournalRecord
from hermes.replay.fingerprint import digest

DECISIONS_SIDECAR = "decisions.json"
DECISIONS_FORMAT = "hermes-decisions"
DECISIONS_VERSION = 1
_REPO = Path(__file__).resolve().parents[2]


def decision_code_fingerprint() -> str:
    """Content hash of the decision layer sources (hermes/decision/*.py) and hermes/config.py."""
    h = hashlib.sha256()
    for p in sorted((_REPO / "hermes" / "decision").glob("*.py")) + [_REPO / "hermes" / "config.py"]:
        h.update(str(p.relative_to(_REPO)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def decision_config_fingerprint(cfg) -> str:
    d = cfg.decision if hasattr(cfg, "decision") else cfg
    return digest(("decision", d))[:16]


@dataclass(slots=True)
class DecisionSet:
    meta: dict
    journal: list[JournalRecord]
    checkpoints: list[DecisionCheckpoint]
    final: DecisionCheckpoint | None
    counts: dict = field(default_factory=dict)

    @classmethod
    def from_runtime(cls, rt: DecisionRuntime, **meta: Any) -> "DecisionSet":
        return cls(dict(meta), list(rt.journal), list(rt.checkpoints), rt.final, rt.counts())


def to_dict(ds: DecisionSet) -> dict:
    return {
        "format": DECISIONS_FORMAT, "version": DECISIONS_VERSION, "journal_schema": JOURNAL_SCHEMA_VERSION,
        **ds.meta, "counts": ds.counts,
        "journal": [r.row() for r in ds.journal],
        "checkpoints": [c.row() for c in ds.checkpoints],
        "final": None if ds.final is None else ds.final.row(),
    }


def save_decisions(rt: DecisionRuntime, path: Path, **meta: Any) -> Path:
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(to_dict(DecisionSet.from_runtime(rt, **meta)), separators=(",", ":")))
    tmp.replace(path)
    return path


def load_decisions(path: Path) -> DecisionSet:
    d = json.loads(Path(path).read_text())
    if d.get("format") != DECISIONS_FORMAT or d.get("version") != DECISIONS_VERSION:
        raise ValueError(f"{path}: not a Hermès decisions file (format/version)")
    meta = {k: v for k, v in d.items() if k not in ("journal", "checkpoints", "final", "counts")}
    return DecisionSet(meta, [JournalRecord.from_row(r) for r in d["journal"]],
                       [DecisionCheckpoint(*c) for c in d["checkpoints"]],
                       None if d.get("final") is None else DecisionCheckpoint(*d["final"]), d.get("counts", {}))


@dataclass(slots=True)
class DecisionCompare:
    evaluations: tuple[int, int] = (0, 0)
    candidates: tuple[int, int] = (0, 0)          # ACTIONABLE creations
    transitions: tuple[int, int] = (0, 0)         # BLOCKED / STALE / INVALIDATED / EXPIRED (+ creation BLOCKED)
    journal: tuple[int, int] = (0, 0)
    journal_matched: int = 0
    checkpoints: tuple[int, int] = (0, 0)
    checkpoints_matched: int = 0
    setup_ids_match: bool = False
    proposal_ids_match: bool = False
    view_ids_match: bool = False
    reason_codes_match: bool = False
    fingerprints_match: bool = False
    final_compared: bool = False
    final_match: bool = False
    limited_to_seq: int | None = None
    first_mismatch: tuple | None = None

    @property
    def equivalent(self) -> bool:
        return (self.journal[0] == self.journal[1] == self.journal_matched
                and self.checkpoints[0] == self.checkpoints[1] == self.checkpoints_matched
                and self.setup_ids_match and self.proposal_ids_match and self.view_ids_match
                and self.reason_codes_match and self.fingerprints_match
                and (self.final_match or not self.final_compared))

    def lines(self) -> list[str]:
        def xy(t):
            return f"{t[0]}/{t[1]}"
        return [
            f"evaluations {xy(self.evaluations)}  candidates {xy(self.candidates)}  "
            f"lifecycle transitions {xy(self.transitions)}",
            f"journal records {self.journal_matched}/{max(self.journal)} match  "
            f"decision checkpoints {self.checkpoints_matched}/{max(self.checkpoints)} match",
            f"setup_ids {'match' if self.setup_ids_match else 'DIFF'}  proposal_ids "
            f"{'match' if self.proposal_ids_match else 'DIFF'}  approval_view_ids "
            f"{'match' if self.view_ids_match else 'DIFF'}  reason codes {'match' if self.reason_codes_match else 'DIFF'}  "
            f"decision fingerprints {'match' if self.fingerprints_match else 'DIFF'}  final "
            f"{'match' if self.final_match else ('n/a' if not self.final_compared else 'DIFF')}",
        ]


_TRANSITIONS = {"CANDIDATE_BLOCKED", "CANDIDATE_STALE", "CANDIDATE_INVALIDATED", "CANDIDATE_EXPIRED"}


def compare_decisions(a: DecisionSet, b: DecisionSet, limit_seq: int | None = None) -> DecisionCompare:
    """Record-by-record comparison. ``limit_seq`` restricts the claim to a verified prefix."""
    ja = [r for r in a.journal if limit_seq is None or r.seq <= limit_seq]
    jb = [r for r in b.journal if limit_seq is None or r.seq <= limit_seq]
    ca = [c for c in a.checkpoints if limit_seq is None or c.seq <= limit_seq]
    cb = [c for c in b.checkpoints if limit_seq is None or c.seq <= limit_seq]

    def count(j, kind):
        return sum(1 for r in j if r.kind.value == kind)

    def trans(j):
        return sum(1 for r in j if r.kind.value in _TRANSITIONS)

    r = DecisionCompare(limited_to_seq=limit_seq)
    r.evaluations = (count(ja, "DECISION_EVALUATED"), count(jb, "DECISION_EVALUATED"))
    r.candidates = (count(ja, "CANDIDATE_ACTIONABLE"), count(jb, "CANDIDATE_ACTIONABLE"))
    r.transitions = (trans(ja), trans(jb))
    r.journal = (len(ja), len(jb))
    r.checkpoints = (len(ca), len(cb))
    for x, y in zip(ja, jb):
        if x == y:
            r.journal_matched += 1
        elif r.first_mismatch is None:
            r.first_mismatch = ("journal", x.seq, x.kind.value, y.seq, y.kind.value)
    for x, y in zip(ca, cb):
        if x == y:
            r.checkpoints_matched += 1
        elif r.first_mismatch is None:
            r.first_mismatch = ("checkpoint", x.seq, x.kind, y.seq, y.kind)
    if r.first_mismatch is None and (len(ja) != len(jb) or len(ca) != len(cb)):
        r.first_mismatch = ("length", len(ja), len(jb), len(ca), len(cb))
    r.setup_ids_match = [x.setup_id for x in ja] == [y.setup_id for y in jb]
    r.proposal_ids_match = [x.proposal_id for x in ja] == [y.proposal_id for y in jb]
    r.view_ids_match = [x.approval_view_id for x in ja] == [y.approval_view_id for y in jb]
    r.reason_codes_match = [x.reasons for x in ja] == [y.reasons for y in jb]
    r.fingerprints_match = ([x.decision_fingerprint for x in ja] == [y.decision_fingerprint for y in jb]
                            and [c.fingerprint for c in ca] == [c.fingerprint for c in cb])
    if a.final is not None and b.final is not None and (
            limit_seq is None or max(a.final.seq, b.final.seq) <= limit_seq):
        r.final_compared = True
        r.final_match = a.final == b.final
        if not r.final_match and r.first_mismatch is None:
            r.first_mismatch = ("final", a.final.seq, a.final.fingerprint, b.final.seq, b.final.fingerprint)
    return r


def decision_meta(cfg, market_code_fingerprint: str, market_config_fingerprint: str, **extra: Any) -> dict:
    return {"decision_code_fingerprint": decision_code_fingerprint(),
            "decision_config_fingerprint": decision_config_fingerprint(cfg),
            "code_fingerprint": market_code_fingerprint, "config_fingerprint": market_config_fingerprint,
            "decision_config": dataclasses.asdict(cfg.decision), **extra}
