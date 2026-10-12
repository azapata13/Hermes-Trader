"""V1 Pro D3/D4/D6 — setup lifecycle tracking, outcome (MFE/MAE) and re-arm. Analytics only.

The C9 ``LifecycleTracker`` owns the ENTRY WINDOW of a candidate (ACTIONABLE -> STALE / EXPIRED /
INVALIDATED / BLOCKED). It is unchanged and stays the only authority for approval. This module
follows the SETUP behind a candidate after its window closed, because STALE != DEAD: "the old entry
window is gone" does not mean "the market stopped being interesting".

Setup phases (``SetupPhase``)::

    ACTIVE       the C9 candidate is ACTIONABLE (its entry window is open)
    MONITORING   the window closed (STALE / EXPIRED / BLOCKED); the setup is still followed
    REARMED      while MONITORING, a NEW ACTIONABLE candidate of the same direction was created by the
                 full candidate engine + SafetyPolicy at a later 30 s decision point (fresh trigger,
                 fresh entry / stop / risk, new setup_id). The old setup only records the link.
    INVALIDATED  the original stop was touched by an eligible print, or C9 invalidated the window
    ENDED        the outcome horizon was reached, or the measurement became incomplete

Outcome (``Outcome``): a SHADOW measurement. It assumes a hypothetical fill at the proposed entry
reference at creation time and measures eligible prints only (never the book, never a synthetic
price). It is never a real trade and never P&L: V1 has no execution layer. Excursions freeze at the
first stop touch (stop-first). A tape continuity break (new tape epoch: reconnect, trades
resubscription), a missing instrument or the end of the session makes it INCOMPLETE — incomplete
outcomes are kept but excluded from statistics; nothing is ever interpolated.

Invariants:
- read-only on the MarketEngine and on the C9 lifecycle; nothing here can change a C9 status,
  create a candidate, move an entry, a stop or a risk, or make anything approvable;
- event time only (``recv_wall_ns``), events processed once in seq order (duplicates / out-of-order
  raw events ignored), so live and replay produce the same tracking journal and fingerprint;
- O(1) per event when nothing is tracked; otherwise O(tracked setups), bounded.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum

from hermes.decision.candidate import Direction
from hermes.decision.lifecycle import CandidateRecord, CandidateStatus, Transition

TRACKING_SCHEMA_VERSION = 1
_S = 1_000_000_000


class SetupPhase(str, Enum):
    ACTIVE = "ACTIVE"
    MONITORING = "MONITORING"
    REARMED = "REARMED"
    INVALIDATED = "INVALIDATED"
    ENDED = "ENDED"


class OutcomeStatus(str, Enum):
    OPEN = "OPEN"              # still measuring
    STOPPED = "STOPPED"        # the original stop was touched (excursions frozen there)
    COMPLETE = "COMPLETE"      # the horizon was reached without stop touch
    INCOMPLETE = "INCOMPLETE"  # data gap / session end: excluded from statistics


FINAL_OUTCOMES = frozenset({OutcomeStatus.STOPPED, OutcomeStatus.COMPLETE, OutcomeStatus.INCOMPLETE})


@dataclass(frozen=True, slots=True)
class TrackingConfig:
    horizons_s: tuple[int, ...] = (60, 180, 300, 600, 900)   # result checkpoints after creation
    max_r: int = 3                                           # +1R .. +3R first-touch times
    max_tracked: int = 64                                    # open setups followed at once (oldest dropped)
    history: int = 5000                                      # finished setups kept in memory

    @property
    def horizon_s(self) -> int:
        return max(self.horizons_s)


@dataclass(frozen=True, slots=True)
class Outcome:
    status: OutcomeStatus = OutcomeStatus.OPEN
    mfe_units: int = 0                       # max favorable excursion vs the original entry (>= 0)
    mae_units: int = 0                       # max adverse excursion (>= 0)
    mfe_wall_ns: int | None = None           # first time the MFE value was reached
    mae_wall_ns: int | None = None
    stop_touched: bool = False
    stop_wall_ns: int | None = None
    r_wall_ns: tuple[int | None, ...] = ()   # first time +kR was reached, k = 1..max_r
    horizon_units: tuple[int | None, ...] = ()   # signed result at each horizon (first print at/after it)
    mfe_after_close_units: int | None = None     # max favorable vs ORIGINAL entry after the window closed
    last_price_units: int | None = None
    prints: int = 0
    incomplete_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SetupView:
    """Immutable snapshot of one tracked setup (what reports, Slack and the LLM context read)."""
    schema_version: int
    setup_id: str
    proposal_id: str | None
    instrument_id: int
    symbol: str
    direction: str
    units_per_point: int | None
    point_value_usd: float
    original_entry_units: int
    original_stop_units: int
    risk_units: int
    created_seq: int
    created_wall_ns: int
    expires_at_ns: int | None
    tape_epoch: int | None
    phase: SetupPhase
    window_close_status: str | None = None       # C9 status that closed the entry window
    window_close_reasons: tuple[str, ...] = ()
    window_closed_wall_ns: int | None = None
    monitoring_start_wall_ns: int | None = None
    invalidation_reason: str | None = None
    invalidated_wall_ns: int | None = None
    rearmed_by: str | None = None                # new setup_id that re-armed this one
    rearm_wall_ns: int | None = None
    rearm_entry_units: int | None = None         # the NEW candidate's entry (never this setup's entry)
    rearm_of: str | None = None                  # for a re-armed candidate: the setup it follows
    ended_wall_ns: int | None = None
    outcome: Outcome = field(default_factory=Outcome)
    features: tuple[tuple[str, str], ...] = ()   # D7 comparable characteristics (strings only)

    # ---- presentation helpers (pure)
    def points(self, units: int | None) -> float | None:
        if units is None or not self.units_per_point:
            return None
        return units / self.units_per_point

    def usd(self, units: int | None) -> float | None:
        p = self.points(units)
        return None if p is None else p * self.point_value_usd

    @property
    def risk_points(self) -> float | None:
        return self.points(self.risk_units)

    def r_multiple(self, units: int | None) -> float | None:
        if units is None or not self.risk_units:
            return None
        return units / self.risk_units

    @property
    def finished(self) -> bool:
        return self.outcome.status in FINAL_OUTCOMES

    def row(self) -> dict:
        """JSON-safe, stable representation (persistence / sidecar)."""
        o = self.outcome
        return {
            "schema": self.schema_version, "setup_id": self.setup_id, "proposal_id": self.proposal_id,
            "instrument_id": self.instrument_id, "symbol": self.symbol, "direction": self.direction,
            "units_per_point": self.units_per_point, "point_value_usd": self.point_value_usd,
            "entry_units": self.original_entry_units, "stop_units": self.original_stop_units,
            "risk_units": self.risk_units, "created_seq": self.created_seq, "created_wall_ns": self.created_wall_ns,
            "expires_at_ns": self.expires_at_ns, "phase": self.phase.value,
            "window_close_status": self.window_close_status, "window_close_reasons": list(self.window_close_reasons),
            "window_closed_wall_ns": self.window_closed_wall_ns, "monitoring_start_wall_ns": self.monitoring_start_wall_ns,
            "invalidation_reason": self.invalidation_reason, "invalidated_wall_ns": self.invalidated_wall_ns,
            "rearmed_by": self.rearmed_by, "rearm_wall_ns": self.rearm_wall_ns,
            "rearm_entry_units": self.rearm_entry_units, "rearm_of": self.rearm_of, "ended_wall_ns": self.ended_wall_ns,
            "outcome": {
                "status": o.status.value, "mfe_units": o.mfe_units, "mae_units": o.mae_units,
                "mfe_wall_ns": o.mfe_wall_ns, "mae_wall_ns": o.mae_wall_ns, "stop_touched": o.stop_touched,
                "stop_wall_ns": o.stop_wall_ns, "r_wall_ns": list(o.r_wall_ns), "horizon_units": list(o.horizon_units),
                "mfe_after_close_units": o.mfe_after_close_units, "last_price_units": o.last_price_units,
                "prints": o.prints, "incomplete_reason": o.incomplete_reason,
            },
            "features": dict(self.features),
            "execution": "SHADOW",            # V1: no execution layer -> every setup is a shadow measurement
        }


@dataclass(frozen=True, slots=True)
class SetupEvent:
    """One audited tracking transition (compact, deterministic)."""
    seq: int
    wall_ns: int
    setup_id: str
    kind: str                 # TRACKED / WINDOW_CLOSED / REARMED / REARM_OF / INVALIDATED / OUTCOME_FINAL
    from_phase: str | None
    to_phase: str
    detail: str = ""

    def row(self) -> list:
        return [self.seq, self.wall_ns, self.setup_id, self.kind, self.from_phase, self.to_phase, self.detail]


class SetupTracker:
    """Single writer: the decision runtime (live pipeline consumer / replay observer)."""

    def __init__(self, point_value_usd: float = 2.0, cfg: TrackingConfig | None = None,
                 on_event: Callable[[SetupEvent, SetupView], None] | None = None,
                 feature_fn: Callable[[CandidateRecord], tuple[tuple[str, str], ...]] | None = None) -> None:
        self.cfg = cfg or TrackingConfig()
        self.point_value_usd = point_value_usd
        self.on_event = on_event                       # read-only hook (Slack / logging); never gates anything
        self.feature_fn = feature_fn
        self.open: dict[str, SetupView] = {}           # setup_id -> view (not finished)
        self.finished: deque[SetupView] = deque(maxlen=self.cfg.history)
        self.by_id: dict[str, SetupView] = {}
        self.events: list[SetupEvent] = []
        self.last_seq = 0
        self.dropped = 0
        self._last_print: dict[str, int] = {}          # setup_id -> seq of the last print applied
        self._chain = "0" * 64

    # ------------------------------------------------------------------ inputs (runtime only)
    def on_actionable(self, rec: CandidateRecord, proposal_id: str | None, raw) -> SetupView | None:
        """A NEW C9 candidate became ACTIONABLE (fresh full validation). Start tracking it, and link it
        as the re-arm of the most recent MONITORING setup of the same direction, if any."""
        c = rec.candidate
        if (rec.status is not CandidateStatus.ACTIONABLE or rec.setup_id in self.by_id
                or c.direction is Direction.NONE or c.entry_reference is None or c.proposed_stop is None):
            return None                               # duplicate registration or not a priced proposal
        risk = abs(c.entry_reference - c.proposed_stop)
        if risk <= 0:
            return None
        n = len(self.cfg.horizons_s)
        prior = self._rearm_candidate(c.direction.value, raw.recv_wall_ns)
        view = SetupView(
            TRACKING_SCHEMA_VERSION, rec.setup_id, proposal_id, c.instrument_id, c.symbol, c.direction.value,
            c.units_per_point, self.point_value_usd, c.entry_reference, c.proposed_stop, risk,
            rec.created_seq, rec.created_wall_ns, rec.expires_at_ns, None, SetupPhase.ACTIVE,
            rearm_of=prior.setup_id if prior is not None else None,
            outcome=Outcome(r_wall_ns=(None,) * self.cfg.max_r, horizon_units=(None,) * n),
            features=self.feature_fn(rec) if self.feature_fn is not None else (),
        )
        if len(self.open) >= self.cfg.max_tracked:
            oldest = min(self.open.values(), key=lambda v: (v.created_seq, v.setup_id))
            self._finish(oldest, OutcomeStatus.INCOMPLETE, "tracking_capacity", raw)
        self._put(view)
        self._event(raw, view.setup_id, "TRACKED", None, SetupPhase.ACTIVE.value,
                    f"entry={c.entry_reference} stop={c.proposed_stop} risk={risk}"
                    + (f" rearm_of={prior.setup_id}" if prior is not None else ""), view)
        if prior is not None:
            new_prior = replace(prior, phase=SetupPhase.REARMED, rearmed_by=view.setup_id,
                                rearm_wall_ns=raw.recv_wall_ns, rearm_entry_units=c.entry_reference)
            self._put(new_prior)
            self._event(raw, prior.setup_id, "REARMED", prior.phase.value, SetupPhase.REARMED.value,
                        f"by={view.setup_id} fresh_entry={c.entry_reference}", new_prior)
        return view

    def on_transitions(self, transitions: list[Transition], raw) -> None:
        """C9 closed entry windows at this event."""
        for t in transitions:
            v = self.open.get(t.setup_id)
            if v is None or v.phase is not SetupPhase.ACTIVE:
                continue
            reasons = tuple(r.code for r in t.reasons)
            if t.to_status is CandidateStatus.INVALIDATED:
                nv = replace(v, phase=SetupPhase.INVALIDATED, window_close_status=t.to_status.value,
                             window_close_reasons=reasons, window_closed_wall_ns=t.wall_ns,
                             invalidation_reason="structural_invalidation", invalidated_wall_ns=t.wall_ns)
                self._put(nv)
                self._event(raw, v.setup_id, "INVALIDATED", v.phase.value, nv.phase.value,
                            "c9:" + ",".join(reasons), nv)
                continue
            nv = replace(v, phase=SetupPhase.MONITORING, window_close_status=t.to_status.value,
                         window_close_reasons=reasons, window_closed_wall_ns=t.wall_ns,
                         monitoring_start_wall_ns=t.wall_ns,
                         outcome=replace(v.outcome, mfe_after_close_units=0))
            self._put(nv)
            self._event(raw, v.setup_id, "WINDOW_CLOSED", v.phase.value, nv.phase.value,
                        f"{t.to_status.value}:{','.join(reasons)}", nv)

    def after_event(self, engine, raw) -> None:
        """Measure every open setup at this event (event time). O(1) when nothing is open."""
        if raw.seq <= self.last_seq:
            return                                    # duplicate / out-of-order raw event: applied once only
        self.last_seq = raw.seq
        if not self.open:
            return
        insts = engine.instruments
        for sid in sorted(self.open, key=lambda s: (self.open[s].created_seq, s)):
            v = self.open.get(sid)
            if v is None:
                continue
            inst = insts.get(v.instrument_id)
            if inst is None or inst.tape is None:
                self._finish(v, OutcomeStatus.INCOMPLETE, "instrument_missing", raw)
                continue
            last = inst.tape.last()
            if v.tape_epoch is None:                   # anchor continuity at the first observation
                v = replace(v, tape_epoch=inst.tape.epoch)
                self._put(v)
            if inst.tape.epoch != v.tape_epoch:
                self._finish(v, OutcomeStatus.INCOMPLETE, "tape_continuity_broken", raw)
                continue
            if (last is not None and last.eligible and last.recv_wall_ns >= v.created_wall_ns
                    and last.tape_epoch == v.tape_epoch and last.seq > self._last_print.get(sid, v.created_seq)):
                self._last_print[sid] = last.seq
                v = self._apply_print(v, last.price_units, last.recv_wall_ns, raw)
                if v is None:
                    continue
            if raw.recv_wall_ns - v.created_wall_ns >= self.cfg.horizon_s * _S:
                self._finish(v, OutcomeStatus.COMPLETE, None, raw)

    def finalize(self, raw_seq: int, wall_ns: int) -> None:
        """Session end: whatever is still open is INCOMPLETE (never extrapolated)."""
        marker = _Raw(raw_seq, wall_ns)
        for sid in sorted(self.open, key=lambda s: (self.open[s].created_seq, s)):
            self._finish(self.open[sid], OutcomeStatus.INCOMPLETE, "session_ended", marker)

    # ------------------------------------------------------------------ measurement
    def _apply_print(self, v: SetupView, price: int, wall_ns: int, raw) -> SetupView | None:
        d = 1 if v.direction == Direction.LONG.value else -1
        o = v.outcome
        fav = d * (price - v.original_entry_units)
        adv = -fav
        mfe, mfe_t, mae, mae_t = o.mfe_units, o.mfe_wall_ns, o.mae_units, o.mae_wall_ns
        if fav > mfe:
            mfe, mfe_t = fav, wall_ns
        if adv > mae:
            mae, mae_t = adv, wall_ns
        r_times = list(o.r_wall_ns)
        for k in range(1, self.cfg.max_r + 1):
            if r_times[k - 1] is None and fav >= k * v.risk_units:
                r_times[k - 1] = wall_ns
        hz = list(o.horizon_units)
        elapsed = wall_ns - v.created_wall_ns
        for i, h in enumerate(self.cfg.horizons_s):
            if hz[i] is None and elapsed >= h * _S:
                hz[i] = fav
        after = o.mfe_after_close_units
        if v.phase is not SetupPhase.ACTIVE and after is not None and fav > after:
            after = fav
        stopped = d * (price - v.original_stop_units) <= 0
        o2 = replace(o, mfe_units=mfe, mfe_wall_ns=mfe_t, mae_units=mae, mae_wall_ns=mae_t,
                     r_wall_ns=tuple(r_times), horizon_units=tuple(hz), mfe_after_close_units=after,
                     last_price_units=price, prints=o.prints + 1)
        v = replace(v, outcome=o2)
        self._put(v)
        if stopped:
            v = replace(v, outcome=replace(o2, stop_touched=True, stop_wall_ns=wall_ns))
            if v.phase is not SetupPhase.INVALIDATED:
                prev = v.phase
                v = replace(v, phase=SetupPhase.INVALIDATED, invalidation_reason="stop_touched",
                            invalidated_wall_ns=wall_ns)
                self._put(v)
                self._event(raw, v.setup_id, "INVALIDATED", prev.value, v.phase.value,
                            f"stop_touched price={price} stop={v.original_stop_units}", v)
            self._finish(v, OutcomeStatus.STOPPED, None, raw)
            return None
        return v

    def _finish(self, v: SetupView, status: OutcomeStatus, reason: str | None, raw) -> None:
        prev = v.phase
        phase = SetupPhase.INVALIDATED if prev is SetupPhase.INVALIDATED else SetupPhase.ENDED
        nv = replace(v, phase=phase, ended_wall_ns=raw.recv_wall_ns,
                     outcome=replace(v.outcome, status=status, incomplete_reason=reason))
        self.open.pop(v.setup_id, None)
        self._last_print.pop(v.setup_id, None)
        self.by_id[v.setup_id] = nv
        self.finished.append(nv)
        self._event(raw, v.setup_id, "OUTCOME_FINAL", prev.value, phase.value,
                    status.value + (f":{reason}" if reason else ""), nv)

    def _rearm_candidate(self, direction: str, wall_ns: int) -> SetupView | None:
        best = None
        for v in self.open.values():
            if (v.phase is SetupPhase.MONITORING and v.direction == direction
                    and wall_ns - v.created_wall_ns < self.cfg.horizon_s * _S
                    and (best is None or (v.created_seq, v.setup_id) > (best.created_seq, best.setup_id))):
                best = v
        return best

    # ------------------------------------------------------------------ bookkeeping
    def _put(self, v: SetupView) -> None:
        self.by_id[v.setup_id] = v
        if not v.finished:
            self.open[v.setup_id] = v

    def _event(self, raw, sid: str, kind: str, frm: str | None, to: str, detail: str, view: SetupView) -> None:
        from hermes.replay.fingerprint import digest
        e = SetupEvent(raw.seq, raw.recv_wall_ns, sid, kind, frm, to, detail)
        self._chain = digest((self._chain, e.row()))
        if len(self.events) < 200_000:
            self.events.append(e)
        else:
            self.dropped += 1
        if self.on_event is not None:
            self.on_event(e, view)

    # ------------------------------------------------------------------ views
    def get(self, sid: str) -> SetupView | None:
        return self.by_id.get(sid)

    def fingerprint(self) -> str:
        from hermes.replay.fingerprint import digest
        return digest(("hermes-setup-tracking", TRACKING_SCHEMA_VERSION, self.cfg, self.point_value_usd,
                       tuple(sorted(self.open)), self._chain, len(self.events) + self.dropped))

    def counts(self) -> dict:
        by_phase: dict[str, int] = {}
        by_outcome: dict[str, int] = {}
        for v in self.by_id.values():
            by_phase[v.phase.value] = by_phase.get(v.phase.value, 0) + 1
            by_outcome[v.outcome.status.value] = by_outcome.get(v.outcome.status.value, 0) + 1
        return {"tracked": len(self.by_id), "open": len(self.open), "events": len(self.events),
                "by_phase": dict(sorted(by_phase.items())), "by_outcome": dict(sorted(by_outcome.items()))}


@dataclass(frozen=True, slots=True)
class _Raw:
    seq: int
    recv_wall_ns: int
