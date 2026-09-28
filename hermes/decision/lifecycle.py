"""C9c — deterministic candidate LIFECYCLE / validity (proposal state only; no execution).

Every SetupCandidate gets a deterministic ``setup_id`` and an immutable ``CandidateRecord`` with an
explicit status:

    NONE         no directional setup at this decision point
    BLOCKED      a hard safety gate failed (at evaluation, or later while it was actionable)
    ACTIONABLE   a LONG/SHORT proposal that may be shown for HUMAN approval
    EXPIRED      event time passed trigger_bar_end + candidate_ttl_seconds
    STALE        the executable reference drifted > max_entry_drift_ticks from the proposed entry
    INVALIDATED  price reached the structural invalidation before approval

Lifecycle status is separate from ``approval_allowed_now``: an ACTIONABLE candidate is
temporarily NOT approvable while the book, though still authoritatively VALID and continuity-
safe, is momentarily non-priceable (crossed / unsorted / empty side during row-by-row depth
updates). Such a snapshot is never used as a price observation — no entry drift and no book-based
structural invalidation are judged from it, and no bid/ask is synthesised from it. When a coherent
VALID book returns, evaluation resumes; this is not a revival (the status never left ACTIONABLE).
Final statuses always have ``approval_allowed_now = False``.

Only ACTIONABLE records ever change status, and only to a terminal status. Nothing is revived, the
entry is never moved, the invalidation level is never moved and risk is never widened; a later
30 s decision point may produce a NEW candidate with a new setup_id. Safety dominates: when
several conditions fire on the same event the order is BLOCKED > INVALIDATED > STALE > EXPIRED.

All checks read the MarketEngine (read-only) and use recorded event time (``recv_wall_ns``) — no
clocks — so live and replay produce the same transitions. Informational annotations (future
memory / shadow / LLM evidence) can be attached with ``annotate`` but can never change a status.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from enum import Enum

from hermes.config import DecisionConfig
from hermes.decision.candidate import Direction, SetupCandidate
from hermes.market.events import BookSide, ConnectionState, Stream
from hermes.market.orderbook import BookState

LIFECYCLE_SCHEMA_VERSION = 1
_S = 1_000_000_000
_REASON_PREFIXES = ("gate", "risk", "regime_5m", "setup_1m", "trigger_30s")


class CandidateStatus(str, Enum):
    NONE = "NONE"
    BLOCKED = "BLOCKED"
    ACTIONABLE = "ACTIONABLE"
    EXPIRED = "EXPIRED"
    STALE = "STALE"
    INVALIDATED = "INVALIDATED"


TERMINAL = frozenset({CandidateStatus.NONE, CandidateStatus.BLOCKED, CandidateStatus.EXPIRED,
                      CandidateStatus.STALE, CandidateStatus.INVALIDATED})
_SEVERITY = {CandidateStatus.BLOCKED: 4, CandidateStatus.INVALIDATED: 3, CandidateStatus.STALE: 2,
             CandidateStatus.EXPIRED: 1}


@dataclass(frozen=True, slots=True)
class StatusReason:
    code: str                          # machine-readable, e.g. "safety:book_not_valid", "entry_drift"
    detail: str


@dataclass(frozen=True, slots=True)
class Transition:
    setup_id: str
    from_status: CandidateStatus
    to_status: CandidateStatus
    seq: int
    wall_ns: int
    reasons: tuple[StatusReason, ...]


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    schema_version: int
    setup_id: str
    candidate: SetupCandidate          # never mutated (entry / stop / invalidation are frozen)
    status: CandidateStatus
    reasons: tuple[StatusReason, ...]
    created_seq: int
    created_wall_ns: int
    expires_at_ns: int | None          # trigger bar end + TTL (event time); None when no trigger bar
    status_seq: int
    status_wall_ns: int
    transitions: tuple[Transition, ...] = ()
    annotations: tuple[str, ...] = ()  # informational only; never affects status
    approval_allowed_now: bool = False # ACTIONABLE and no temporary hold at the last observation
    temporary_hold_reasons: tuple[StatusReason, ...] = ()   # e.g. crossed_book_transition

    @property
    def actionable(self) -> bool:
        return self.status is CandidateStatus.ACTIONABLE


# ---------------------------------------------------------------------------- identity

def setup_id(c: SetupCandidate) -> str:
    """Deterministic identity from stable facts only (no wall clock, no randomness, no process state)."""
    from hermes.replay.fingerprint import digest
    r5 = c.regime_5m.bar_context.latest if c.regime_5m.bar_context is not None else None
    s1 = c.setup_1m.bar_context.latest if c.setup_1m.bar_context is not None else None
    facts = ("hermes-setup-v1", c.instrument_id, c.direction.value,
             r5.end_s if r5 is not None else None, s1.end_s if s1 is not None else None,
             c.trigger_bar_end_s, c.structural_invalidation)
    return "S" + digest(facts)[:23]


def _reason(text: str) -> StatusReason:
    parts = text.split(":", 2)
    if parts[0] in _REASON_PREFIXES and len(parts) >= 2:
        return StatusReason(f"{parts[0]}:{parts[1]}", parts[2] if len(parts) > 2 else "")
    return StatusReason(parts[0], text[len(parts[0]) + 1:] if len(parts) > 1 else "")


def initial_record(c: SetupCandidate, cfg: DecisionConfig) -> CandidateRecord:
    if c.direction is not Direction.NONE:
        status, reasons = CandidateStatus.ACTIONABLE, (StatusReason("actionable_proposal", "HUMAN_APPROVAL only"),)
    else:
        reasons = tuple(_reason(r) for r in c.blocking_reasons)
        safety = any(r.code.startswith("gate:") or r.code in ("outside_authorized_entry_hours",
                                                                "trigger_evaluation_stale",
                                                                "no_completed_30s_trigger_bar")
                     for r in reasons)
        status = CandidateStatus.BLOCKED if safety else CandidateStatus.NONE
    exp = None if c.trigger_bar_end_s is None else (c.trigger_bar_end_s + cfg.candidate_ttl_seconds) * _S
    return CandidateRecord(LIFECYCLE_SCHEMA_VERSION, setup_id(c), c, status, reasons, c.seq, c.wall_ns, exp,
                           c.seq, c.wall_ns, approval_allowed_now=status is CandidateStatus.ACTIONABLE)


def book_coherence(book) -> tuple[bool, str]:
    """Is the current depth snapshot usable as an executable price observation?

    Independent of the authoritative book state (VALID/SUSPECT/STALE is judged by the OrderBook):
    a VALID book can be momentarily crossed or unsorted between row updates."""
    if book is None:
        return False, "no_book"
    bids = book.levels(BookSide.BID)
    asks = book.levels(BookSide.ASK)
    if not bids or not asks:
        return False, "empty_side_transition"
    if bids[0][0] >= asks[0][0]:
        return False, "crossed_book_transition"
    if any(bids[i][0] <= bids[i + 1][0] for i in range(len(bids) - 1)) or \
            any(asks[i][0] >= asks[i + 1][0] for i in range(len(asks) - 1)):
        return False, "unsorted_book_transition"
    return True, "ok"


# ---------------------------------------------------------------------------- checks (read-only)

_EPOCH_READERS = {
    "tape_epoch": lambda i: i.tape.epoch if i.tape is not None else None,
    "classifier_epoch": lambda i: i.classifier.epoch if i.classifier is not None else None,
    "metrics_epoch": lambda i: i.metrics.continuity_epoch,
    "structure_epoch": lambda i: i.structure.continuity_epoch,
    "pattern_epoch": lambda i: i.patterns.continuity_epoch,
    "book_epoch": lambda i: i.book.epoch if i.book is not None else None,
}


def safety_violations(engine, inst, c: SetupCandidate, cfg: DecisionConfig) -> list[StatusReason]:
    """Hard safety conditions re-checked against the live engine state. Any => BLOCKED."""
    out: list[StatusReason] = []
    if engine.connection is not ConnectionState.CONNECTED:
        out.append(StatusReason("safety:connection", engine.connection.value))
    if engine.not_live:
        out.append(StatusReason("safety:market_data_not_live", "delayed/frozen data"))
    if engine.conflict.active:
        out.append(StatusReason("safety:session_conflict_10197", engine.conflict.phase.value))
    if engine.farm_broken:
        out.append(StatusReason("safety:farm_broken", ""))
    if engine.alerts:
        out.append(StatusReason("safety:critical_alert", ",".join(sorted(engine.alerts))))
    if inst.book is None or inst.book.state is not BookState.VALID:
        out.append(StatusReason("safety:book_not_valid", inst.book.state.value if inst.book else "none"))
    for name, gen in c.stream_generations:
        st = inst.streams.get(Stream(name))
        if st is None or st.generation != gen or st.error_active:
            out.append(StatusReason("safety:stream_changed", f"{name}: {gen} -> {st.generation if st else None}"))
    for name, before in c.continuity:
        now = _EPOCH_READERS[name](inst)
        if now != before:
            out.append(StatusReason("safety:continuity_epoch_changed", f"{name}: {before} -> {now}"))
    if inst.bars is not None and inst.bars.cond_flags:
        out.append(StatusReason("safety:active_data_gap", str(inst.bars.cond_flags)))
    if cfg.entry_hours == "RTH_ONLY" and not (inst.sessions is not None and inst.sessions.in_rth):
        out.append(StatusReason("safety:left_rth", "outside_authorized_entry_hours"))
    if inst.grid is None or not inst.grid.is_uniform:
        out.append(StatusReason("safety:price_grid_invalid", ""))
    ok, why = engine.classification_context(inst)
    if not ok:
        out.append(StatusReason("safety:classification_invalid", why))
    reasons = engine.market_data_reasons(inst)
    if reasons:
        out.append(StatusReason("safety:market_data_not_ok", ",".join(reasons[:4])))
    return out


def price_checks(inst, c: SetupCandidate, cfg: DecisionConfig
                 ) -> tuple[list[StatusReason], list[StatusReason], list[StatusReason]]:
    """(structural invalidation, entry drift, temporary holds) at this observation.

    Book-based checks use a COHERENT VALID book only; a momentarily non-priceable snapshot yields a
    temporary hold instead (never a synthetic bid/ask). Touch counts: LONG coherent best bid <=
    invalidation, SHORT coherent best ask >= invalidation. An ELIGIBLE print newer than the
    candidate at/through the level invalidates regardless of the book snapshot."""
    inval: list[StatusReason] = []
    drift: list[StatusReason] = []
    hold: list[StatusReason] = []
    book = inst.book
    lvl = c.structural_invalidation
    if lvl is None:
        return inval, drift, hold
    long_ = c.direction is Direction.LONG
    last = inst.tape.last() if inst.tape is not None else None
    if last is not None and last.eligible and last.recv_mono_ns > c.mono_ns:
        p = last.price_units
        if (long_ and p <= lvl) or (not long_ and p >= lvl):
            inval.append(StatusReason("structural_invalidation",
                                      f"eligible print {p} {'<=' if long_ else '>='} invalidation {lvl}"))
    if book is None or book.state is not BookState.VALID:
        return inval, drift, hold          # authoritative state failure is a SAFETY block (handled elsewhere)
    ok, why = book_coherence(book)
    if not ok:
        hold.append(StatusReason(why, "VALID book momentarily non-priceable; not used as a price observation"))
        return inval, drift, hold
    bb, ba = book.best_bid()[0], book.best_ask()[0]
    if long_ and bb <= lvl:
        inval.append(StatusReason("structural_invalidation", f"best_bid {bb} <= invalidation {lvl}"))
    if not long_ and ba >= lvl:
        inval.append(StatusReason("structural_invalidation", f"best_ask {ba} >= invalidation {lvl}"))
    ref = ba if long_ else bb
    tick = inst.grid.step_at(0) if inst.grid is not None and inst.grid.is_uniform else None
    if tick and c.entry_reference is not None:
        d = abs(ref - c.entry_reference)
        if d > cfg.max_entry_drift_ticks * tick:
            drift.append(StatusReason("entry_drift", f"{'ask' if long_ else 'bid'} {ref} is {d // tick} ticks "
                                      f"from entry {c.entry_reference} (max {cfg.max_entry_drift_ticks})"))
    return inval, drift, hold


# ---------------------------------------------------------------------------- tracker

class LifecycleTracker:
    """Deterministic lifecycle state OUTSIDE MarketEngine (single writer: the decision driver)."""

    def __init__(self, cfg: DecisionConfig | None = None, instrument_id: int | None = None,
                 history: int | None = None) -> None:
        self.cfg = cfg or DecisionConfig()
        self.instrument_id = instrument_id
        self.active: dict[str, CandidateRecord] = {}
        self.records: deque[CandidateRecord] = deque(maxlen=history or self.cfg.candidate_history)
        self.by_id: dict[str, CandidateRecord] = {}
        self.transitions: deque[Transition] = deque(maxlen=history or self.cfg.candidate_history)
        self.counts: dict[str, int] = {}
        self._chain = "0" * 64

    # ---- mutation (driver only)
    def register(self, c: SetupCandidate) -> CandidateRecord:
        rec = initial_record(c, self.cfg)
        self._store(rec)
        if rec.status is CandidateStatus.ACTIONABLE:
            self.active[rec.setup_id] = rec
        self.counts[rec.status.value] = self.counts.get(rec.status.value, 0) + 1
        self._fold(("register", rec.setup_id, rec.status.value, rec.created_seq))
        return rec

    def update(self, engine, seq: int, wall_ns: int) -> list[Transition]:
        """Re-check every ACTIONABLE record at this event. O(1) when nothing is active."""
        if not self.active:
            return []
        insts = engine.instruments
        iid = self.instrument_id
        inst = insts.get(iid) if iid is not None else (next(iter(insts.values())) if insts else None)
        done: list[Transition] = []
        for sid in list(self.active):
            rec = self.active[sid]
            c = rec.candidate
            outcome: tuple[CandidateStatus, list[StatusReason]] | None = None
            hold: list[StatusReason] = []
            if inst is None:
                outcome = (CandidateStatus.BLOCKED, [StatusReason("safety:instrument_missing", "")])
            else:
                safety = safety_violations(engine, inst, c, self.cfg)
                inval, drift, hold = price_checks(inst, c, self.cfg)
                if safety:
                    outcome = (CandidateStatus.BLOCKED, safety)
                elif inval:
                    outcome = (CandidateStatus.INVALIDATED, inval)
                elif drift:
                    outcome = (CandidateStatus.STALE, drift)
                elif rec.expires_at_ns is not None and wall_ns >= rec.expires_at_ns:
                    outcome = (CandidateStatus.EXPIRED,
                               [StatusReason("expired", f"event time passed trigger bar end + "
                                                        f"{self.cfg.candidate_ttl_seconds}s")])
            if outcome is None:
                allowed = not hold
                if allowed != rec.approval_allowed_now or tuple(hold) != rec.temporary_hold_reasons:
                    upd = replace(rec, approval_allowed_now=allowed, temporary_hold_reasons=tuple(hold))
                    self.active[sid] = upd
                    self._store(upd)
                    self._fold(("hold", sid, allowed, tuple(h.code for h in hold), seq))
                continue
            status, reasons = outcome
            t = Transition(sid, rec.status, status, seq, wall_ns, tuple(reasons))
            new = replace(rec, status=status, reasons=tuple(reasons), status_seq=seq, status_wall_ns=wall_ns,
                          transitions=rec.transitions + (t,), approval_allowed_now=False,
                          temporary_hold_reasons=())
            del self.active[sid]
            self._store(new)
            self.transitions.append(t)
            self.counts[status.value] = self.counts.get(status.value, 0) + 1
            self._fold(("transition", sid, status.value, seq, wall_ns, tuple(r.code for r in reasons)))
            done.append(t)
        return done

    def annotate(self, sid: str, note: str) -> CandidateRecord | None:
        """Attach informational evidence (e.g. future memory/shadow/LLM output). NEVER changes
        status: a blocked, stale, invalidated or expired candidate stays exactly that."""
        rec = self.by_id.get(sid)
        if rec is None:
            return None
        new = replace(rec, annotations=rec.annotations + (note,))
        self._store(new)
        if sid in self.active:
            self.active[sid] = new
        return new

    # ---- views
    def get(self, sid: str) -> CandidateRecord | None:
        return self.by_id.get(sid)

    def latest(self) -> CandidateRecord | None:
        return self.records[-1] if self.records else None

    def fingerprint(self) -> str:
        from hermes.replay.fingerprint import digest
        return digest(("hermes-lifecycle", LIFECYCLE_SCHEMA_VERSION, self.cfg.candidate_ttl_seconds,
                       self.cfg.max_entry_drift_ticks, tuple(sorted(self.active)),
                       tuple(sorted(self.counts.items())), self._chain))

    # ---- internals
    def _store(self, rec: CandidateRecord) -> None:
        old = self.by_id.get(rec.setup_id)
        if old is not None:
            try:
                self.records.remove(old)
            except ValueError:
                pass
        if self.records.maxlen is not None and len(self.records) == self.records.maxlen and old is None:
            evicted = self.records[0]
            self.by_id.pop(evicted.setup_id, None)
        self.records.append(rec)
        self.by_id[rec.setup_id] = rec

    def _fold(self, item: tuple) -> None:
        from hermes.replay.fingerprint import digest
        self._chain = digest((self._chain, item))
