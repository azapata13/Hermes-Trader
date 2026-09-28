"""C9 decision driver: feeds the CandidateEngine and LifecycleTracker from a MarketEngine.

After each raw event (live pipeline consumer / replay observer interface ``after_event``):

1. re-check ACTIONABLE candidates against the engine state at this event (expiry, entry drift,
   structural invalidation, safety) — O(1) when nothing is active;
2. if a 30 s bar completed, build ONE immutable snapshot, evaluate ONE candidate and register it.

Read-only on the MarketEngine; never mutates market state; no clock (event time only).
"""

from __future__ import annotations

from hermes.decision.candidate import CandidateEngine, SetupCandidate
from hermes.decision.lifecycle import LifecycleTracker, Transition


class CandidateDriver:
    def __init__(self, engine, candidates: CandidateEngine, lifecycle: LifecycleTracker | None = None) -> None:
        self.engine = engine
        self.candidates = candidates
        self.lifecycle = lifecycle if lifecycle is not None else LifecycleTracker(candidates.cfg,
                                                                                  candidates.instrument_id)
        self.snapshots_built = 0
        self.last_transitions: list[Transition] = []

    def completed_30s(self) -> int:
        iid = self.candidates.instrument_id
        insts = self.engine.instruments
        inst = insts.get(iid) if iid is not None else (next(iter(insts.values())) if insts else None)
        return inst.bars.completed[30] if inst is not None and inst.bars is not None else 0

    def after_event(self, raw, events, now_mono_ns: int) -> SetupCandidate | None:
        if self.lifecycle.active and raw is not None:
            self.last_transitions = self.lifecycle.update(self.engine, raw.seq, raw.recv_wall_ns)
        if not self.candidates.due(self.completed_30s()):
            return None
        self.snapshots_built += 1
        cand = self.candidates.on_snapshot(self.engine.snapshot())
        if cand is not None:
            self.lifecycle.register(cand)
        return cand

    def fingerprint(self) -> str:
        from hermes.replay.fingerprint import digest
        return digest(("hermes-decision-driver", self.candidates.fingerprint(), self.lifecycle.fingerprint()))
