"""C9d — one explicit, deterministic SafetyPolicy for candidate entry.

Every place that decides whether a candidate may exist or be approved uses THIS module:
candidate creation (from a DecisionContext), the lifecycle re-check on every event while a
candidate is ACTIONABLE, and the future approval / execution-adapter check (from the live
MarketEngine). Facts are gathered into a neutral ``SafetyFacts`` value by one of two readers
(``facts_from_context`` / ``facts_from_engine``) and judged by ONE function, so the rules can never
drift apart.

Results are structured:

* ``hard_block_reasons``  — the candidate cannot be entered (stable reason codes below);
* ``temporary_hold_reasons`` — ONLY for a narrowly defined transient non-priceable state: the
  authoritative book is still VALID and continuity-safe but the current depth snapshot is
  momentarily crossed / unsorted / missing a side between row updates.

``allowed`` is True only with no hard block AND no hold. Nothing outside this module — no LLM,
historical memory, shadow strategy or learned rule — can remove either kind of restriction; the
policy accepts no override input. Trading-performance limits (max trades/day, loss limits) are
NOT here: they belong to later risk/execution eligibility state. No clocks: time is event time.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.config import DecisionConfig
from hermes.market.events import BookSide, ConnectionState

SAFETY_SCHEMA_VERSION = 1
_S = 1_000_000_000

# ---- stable reason codes (tested; never rename without a schema bump) ----
CONNECTION_UNUSABLE = "connection_unusable"
CONTRACT_NOT_DEFINED = "contract_not_defined"
MARKET_DATA_NOT_LIVE = "market_data_not_live"
SESSION_CONFLICT_10197 = "session_conflict_10197"
CRITICAL_ALERT = "critical_alert"
FARM_BROKEN = "farm_broken"
REQUIRED_STREAM_INACTIVE = "required_stream_inactive"
STREAM_GENERATION_CHANGED = "stream_generation_changed"
BOOK_NOT_VALID = "book_not_valid"
ACTIVE_DATA_GAP = "active_data_gap"
CONTINUITY_EPOCH_CHANGED = "continuity_epoch_changed"
CLASSIFICATION_CONTEXT_INVALID = "classification_context_invalid"
PRICE_GRID_NOT_UNIFORM = "price_grid_not_uniform"
SESSION_CALENDAR_INVALID = "session_calendar_invalid"
OUTSIDE_AUTHORIZED_ENTRY_HOURS = "outside_authorized_entry_hours"
WITHIN_OPENING_BUFFER = "within_opening_buffer"
WITHIN_CLOSING_BUFFER = "within_closing_buffer"
MARKET_DATA_NOT_OK = "market_data_not_ok"
C7_METRICS_UNAVAILABLE = "c7_metrics_unavailable"
C8_STRUCTURE_UNAVAILABLE = "c8_structure_unavailable"
C8_PATTERNS_UNAVAILABLE = "c8_patterns_unavailable"
CANDIDATE_EXPIRED = "candidate_expired"
CANDIDATE_STATUS_NOT_ACTIONABLE = "candidate_status_not_actionable"
# temporary holds (transient, non-priceable but authoritatively VALID book)
CROSSED_BOOK_TRANSITION = "crossed_book_transition"
UNSORTED_BOOK_TRANSITION = "unsorted_book_transition"
EMPTY_SIDE_TRANSITION = "empty_side_transition"

HARD_CODES = frozenset({
    CONNECTION_UNUSABLE, CONTRACT_NOT_DEFINED, MARKET_DATA_NOT_LIVE, SESSION_CONFLICT_10197, CRITICAL_ALERT,
    FARM_BROKEN, REQUIRED_STREAM_INACTIVE, STREAM_GENERATION_CHANGED, BOOK_NOT_VALID, ACTIVE_DATA_GAP,
    CONTINUITY_EPOCH_CHANGED, CLASSIFICATION_CONTEXT_INVALID, PRICE_GRID_NOT_UNIFORM, SESSION_CALENDAR_INVALID,
    OUTSIDE_AUTHORIZED_ENTRY_HOURS, WITHIN_OPENING_BUFFER, WITHIN_CLOSING_BUFFER, MARKET_DATA_NOT_OK,
    C7_METRICS_UNAVAILABLE, C8_STRUCTURE_UNAVAILABLE, C8_PATTERNS_UNAVAILABLE, CANDIDATE_EXPIRED,
    CANDIDATE_STATUS_NOT_ACTIONABLE,
})
HOLD_CODES = frozenset({CROSSED_BOOK_TRANSITION, UNSORTED_BOOK_TRANSITION, EMPTY_SIDE_TRANSITION})
# authorization-window codes: at CREATION these mean "no candidate here" (NONE), not a failure
WINDOW_CODES = frozenset({OUTSIDE_AUTHORIZED_ENTRY_HOURS, WITHIN_OPENING_BUFFER, WITHIN_CLOSING_BUFFER})

PURPOSE_CREATION = "creation"      # candidate creation from a DecisionContext
PURPOSE_LIFECYCLE = "lifecycle"    # per-event re-check of an ACTIONABLE candidate (TTL/status handled by lifecycle)
PURPOSE_APPROVAL = "approval"      # future approval / execution adapter: everything incl. TTL and status


@dataclass(frozen=True, slots=True)
class StatusReason:
    code: str                          # stable machine code (see constants above) or a lifecycle code
    detail: str


@dataclass(frozen=True, slots=True)
class SessionPolicyResult:
    policy: str                        # "RTH_ONLY"
    calendar_ok: bool
    in_rth: bool
    rth_start_s: int | None
    rth_end_s: int | None
    opening_buffer_s: int
    closing_buffer_s: int
    authorized_from_s: int | None      # rth_start + opening buffer
    authorized_until_s: int | None     # rth_end - closing buffer (exclusive)
    authorized: bool
    reason: str


@dataclass(frozen=True, slots=True)
class SafetyFacts:
    """Neutral inputs for the policy (read from a DecisionContext or from the live engine)."""
    now_wall_ns: int
    connection: str
    contract_state: str
    market_data_type: int | None
    not_live: bool
    conflict_active: bool
    conflict_phase: str
    farm_broken: bool
    alerts: tuple[str, ...]
    required_streams: tuple[str, ...]
    streams: tuple[tuple[str, int | None, str], ...]    # (name, generation, effective status)
    book_state: str | None
    book_coherent: bool
    book_coherence_reason: str
    bar_active_flags: int
    continuity: tuple[tuple[str, int | None], ...]
    classification_ok: bool
    classification_reason: str
    grid_uniform: bool
    calendar_ok: bool
    in_rth: bool
    rth_start_s: int | None
    rth_end_s: int | None
    market_data_ok: bool
    not_ok_reasons: tuple[str, ...]
    c7_available: bool
    c8_structure_available: bool
    c8_patterns_available: bool


@dataclass(frozen=True, slots=True)
class SafetyResult:
    schema_version: int
    purpose: str
    allowed: bool
    hard_block_reasons: tuple[StatusReason, ...]
    temporary_hold_reasons: tuple[StatusReason, ...]
    session_policy: SessionPolicyResult
    continuity: tuple[tuple[str, int | None, int | None], ...]   # (name, baseline, now)
    market_data: tuple[tuple[str, str], ...]                     # compact evidence (key, value)
    evaluated_wall_ns: int = 0          # event time of the facts this result judged
    subject: str | None = None          # setup_id of the judged CandidateRecord (approval purpose)

    @property
    def hard_codes(self) -> frozenset[str]:
        return frozenset(r.code for r in self.hard_block_reasons)

    @property
    def only_window_blocks(self) -> bool:
        return bool(self.hard_block_reasons) and self.hard_codes <= WINDOW_CODES


# ---------------------------------------------------------------------------- coherence

def rows_coherence(bids, asks) -> tuple[bool, str]:
    """Can this depth snapshot be used as an executable price observation?"""
    if not bids or not asks:
        return False, EMPTY_SIDE_TRANSITION
    if bids[0][0] >= asks[0][0]:
        return False, CROSSED_BOOK_TRANSITION
    if any(bids[i][0] <= bids[i + 1][0] for i in range(len(bids) - 1)) or \
            any(asks[i][0] >= asks[i + 1][0] for i in range(len(asks) - 1)):
        return False, UNSORTED_BOOK_TRANSITION
    return True, "ok"


def book_coherence(book) -> tuple[bool, str]:
    if book is None:
        return False, EMPTY_SIDE_TRANSITION
    return rows_coherence(book.levels(BookSide.BID), book.levels(BookSide.ASK))


# ---------------------------------------------------------------------------- readers

def facts_from_context(ctx) -> SafetyFacts:
    q, s = ctx.quality, ctx.session
    ss = s.session
    return SafetyFacts(
        now_wall_ns=ctx.wall_ns, connection=q.connection, contract_state=q.contract_state,
        market_data_type=q.market_data_type, not_live=q.not_live, conflict_active=q.conflict_phase != "none",
        conflict_phase=q.conflict_phase, farm_broken=q.farm_broken, alerts=q.alerts,
        required_streams=q.required_streams, streams=q.streams,
        book_state=q.book_state, book_coherent=q.book_coherent, book_coherence_reason=q.book_coherence_reason,
        bar_active_flags=q.bar_active_flags,
        continuity=(("tape_epoch", q.tape_epoch), ("classifier_epoch", q.classifier_epoch),
                    ("metrics_epoch", q.metrics_epoch), ("structure_epoch", q.structure_epoch),
                    ("pattern_epoch", q.pattern_epoch), ("book_epoch", q.book_epoch)),
        classification_ok=q.classification_context_ok, classification_reason=q.classification_context_reason,
        grid_uniform=ctx.price.units_per_point is not None and ctx.price.tick_units is not None,
        calendar_ok=q.session_calendar_ok and s.available, in_rth=s.in_rth,
        rth_start_s=ss.rth_start_s if ss is not None else None, rth_end_s=ss.rth_end_s if ss is not None else None,
        market_data_ok=q.market_data_ok, not_ok_reasons=q.not_ok_reasons,
        c7_available=ctx.flow.available, c8_structure_available=ctx.structure_section.available,
        c8_patterns_available=ctx.patterns_section.available,
    )


def facts_from_engine(engine, inst, now_wall_ns: int) -> SafetyFacts:
    """Read-only facts from the live/replayed MarketEngine (no snapshot needed: cheap per event)."""
    b = inst.book
    ok, why = book_coherence(b) if b is not None else (False, EMPTY_SIDE_TRANSITION)
    sess = inst.sessions
    cur = sess.current if sess is not None else None
    rw = cur.rth_window if cur is not None else None
    cls_ok, cls_why = engine.classification_context(inst)
    reasons = engine.market_data_reasons(inst)
    return SafetyFacts(
        now_wall_ns=now_wall_ns, connection=engine.connection.value, contract_state=inst.contract_state,
        market_data_type=inst.market_data_type, not_live=engine.not_live, conflict_active=engine.conflict.active,
        conflict_phase=engine.conflict.phase.value, farm_broken=engine.farm_broken,
        alerts=tuple(sorted(engine.alerts)), required_streams=tuple(s.value for s in inst.required),
        streams=tuple((s.value, st.generation, engine.effective_status(st).value) for s, st in inst.streams.items()),
        book_state=b.state.value if b is not None else None, book_coherent=ok, book_coherence_reason=why,
        bar_active_flags=int(inst.bars.cond_flags) if inst.bars is not None else 0,
        continuity=(("tape_epoch", inst.tape.epoch if inst.tape is not None else None),
                    ("classifier_epoch", inst.classifier.epoch if inst.classifier is not None else None),
                    ("metrics_epoch", inst.metrics.continuity_epoch),
                    ("structure_epoch", inst.structure.continuity_epoch),
                    ("pattern_epoch", inst.patterns.continuity_epoch),
                    ("book_epoch", b.epoch if b is not None else None)),
        classification_ok=cls_ok, classification_reason=cls_why,
        grid_uniform=inst.grid is not None and inst.grid.is_uniform,
        calendar_ok=sess is not None and sess.ok, in_rth=bool(sess and sess.in_rth),
        rth_start_s=rw.start_s if rw is not None else None, rth_end_s=rw.end_s if rw is not None else None,
        market_data_ok=not reasons, not_ok_reasons=tuple(reasons),
        c7_available=bool(inst.metrics.token()[1]),                      # C7 book metrics valid
        c8_structure_available=bool(inst.structure.token()[1]), c8_patterns_available=bool(inst.patterns.token()[1]),
    )


# ---------------------------------------------------------------------------- policy

class SafetyPolicy:
    """Judges SafetyFacts. Accepts no override: restrictions can only be ADDED by the facts."""

    def __init__(self, cfg: DecisionConfig | None = None) -> None:
        self.cfg = cfg or DecisionConfig()

    def session_policy(self, f: SafetyFacts) -> SessionPolicyResult:
        cfg = self.cfg
        ob, cb = cfg.opening_buffer_seconds, cfg.closing_buffer_seconds
        start = f.rth_start_s + ob if f.rth_start_s is not None else None
        until = f.rth_end_s - cb if f.rth_end_s is not None else None
        if not f.calendar_ok:
            return SessionPolicyResult(cfg.entry_policy, False, f.in_rth, f.rth_start_s, f.rth_end_s, ob, cb,
                                       start, until, False, SESSION_CALENDAR_INVALID)
        if not f.in_rth or start is None or until is None:
            return SessionPolicyResult(cfg.entry_policy, True, f.in_rth, f.rth_start_s, f.rth_end_s, ob, cb,
                                       start, until, False, OUTSIDE_AUTHORIZED_ENTRY_HOURS)
        now_ns = f.now_wall_ns                                   # exact integer comparisons (no float)
        if now_ns < start * _S:
            reason = WITHIN_OPENING_BUFFER
        elif now_ns >= until * _S:
            reason = WITHIN_CLOSING_BUFFER if now_ns < f.rth_end_s * _S else OUTSIDE_AUTHORIZED_ENTRY_HOURS  # type: ignore[operator]
        else:
            reason = "authorized"
        return SessionPolicyResult(cfg.entry_policy, True, True, f.rth_start_s, f.rth_end_s, ob, cb, start, until,
                                   reason == "authorized", reason)

    def evaluate(self, f: SafetyFacts, *, purpose: str = PURPOSE_CREATION, baseline=None,
                 record=None) -> SafetyResult:
        """``baseline``: the SetupCandidate whose continuity epochs / stream generations must be
        unchanged (lifecycle, approval). ``record``: its CandidateRecord (approval: TTL + status)."""
        hard: list[StatusReason] = []
        hold: list[StatusReason] = []
        H = hard.append
        if f.connection != ConnectionState.CONNECTED.value:
            H(StatusReason(CONNECTION_UNUSABLE, f.connection))
        if f.contract_state != "defined":
            H(StatusReason(CONTRACT_NOT_DEFINED, f.contract_state))
        if f.market_data_type != 1 or f.not_live:
            H(StatusReason(MARKET_DATA_NOT_LIVE, f"market_data_type={f.market_data_type} not_live={f.not_live}"))
        if f.conflict_active:
            H(StatusReason(SESSION_CONFLICT_10197, f.conflict_phase))
        if f.alerts:
            H(StatusReason(CRITICAL_ALERT, ",".join(f.alerts)))
        if f.farm_broken:
            H(StatusReason(FARM_BROKEN, "market data farm broken"))
        st = {name: (gen, status) for name, gen, status in f.streams}
        for name in f.required_streams:
            gen, status = st.get(name, (None, "missing"))
            ok = gen is not None and (status == "active" or (name == "trades" and status in ("active", "requested")))
            if not ok:
                H(StatusReason(REQUIRED_STREAM_INACTIVE, f"{name}: generation={gen} status={status}"))
        if f.book_state != "valid":
            H(StatusReason(BOOK_NOT_VALID, str(f.book_state)))
        elif not f.book_coherent:
            hold.append(StatusReason(f.book_coherence_reason,
                                     "VALID book momentarily non-priceable; not an executable price observation"))
        if f.bar_active_flags:
            H(StatusReason(ACTIVE_DATA_GAP, f"bar quality flags={f.bar_active_flags}"))
        if not f.classification_ok:
            H(StatusReason(CLASSIFICATION_CONTEXT_INVALID, f.classification_reason))
        if not f.grid_uniform:
            H(StatusReason(PRICE_GRID_NOT_UNIFORM, "price grid unknown or not uniform"))
        sp = self.session_policy(f)
        if not sp.authorized:
            H(StatusReason(sp.reason, f"now={f.now_wall_ns // _S} window=[{sp.authorized_from_s}, "
                                      f"{sp.authorized_until_s}) policy={sp.policy}"))
        if not f.market_data_ok:
            H(StatusReason(MARKET_DATA_NOT_OK, ",".join(f.not_ok_reasons[:6])))
        # Current evidence availability is judged at EVERY purpose (creation, lifecycle, approval),
        # independently of continuity epochs: a loss of availability need not bump an epoch.
        if not f.c7_available:
            H(StatusReason(C7_METRICS_UNAVAILABLE, "C7 book metrics unavailable"))
        if not f.c8_structure_available:
            H(StatusReason(C8_STRUCTURE_UNAVAILABLE, "C8 structure continuity unavailable"))
        if not f.c8_patterns_available:
            H(StatusReason(C8_PATTERNS_UNAVAILABLE, "C8 patterns continuity unavailable"))
        cont: list[tuple[str, int | None, int | None]] = []
        if baseline is not None:
            now = dict(f.continuity)
            for name, before in baseline.continuity:
                cont.append((name, before, now.get(name)))
                if now.get(name) != before:
                    H(StatusReason(CONTINUITY_EPOCH_CHANGED, f"{name}: {before} -> {now.get(name)}"))
            for name, gen in baseline.stream_generations:
                g2 = st.get(name, (None, "missing"))[0]
                if g2 != gen:
                    H(StatusReason(STREAM_GENERATION_CHANGED, f"{name}: {gen} -> {g2}"))
        else:
            cont = [(name, None, v) for name, v in f.continuity]
        if purpose == PURPOSE_APPROVAL and record is not None:
            if record.status.value != "ACTIONABLE":
                H(StatusReason(CANDIDATE_STATUS_NOT_ACTIONABLE, record.status.value))
            if record.expires_at_ns is None or f.now_wall_ns >= record.expires_at_ns:
                H(StatusReason(CANDIDATE_EXPIRED, f"expires_at_ns={record.expires_at_ns}"))
        md = (("connection", f.connection), ("market_data_type", str(f.market_data_type)),
              ("not_live", str(f.not_live)), ("conflict_phase", f.conflict_phase), ("book_state", str(f.book_state)),
              ("book_coherent", str(f.book_coherent)), ("market_data_ok", str(f.market_data_ok)))
        subject = getattr(record, "setup_id", None) if record is not None else None
        return SafetyResult(SAFETY_SCHEMA_VERSION, purpose, not hard and not hold, tuple(hard), tuple(hold), sp,
                            tuple(cont), md, f.now_wall_ns, subject)


def approval_check(engine, record, cfg: DecisionConfig | None = None, now_wall_ns: int | None = None,
                   instrument_id: int | None = None) -> SafetyResult:
    """Full approval-time safety for a CandidateRecord against the live engine (event time).
    For the future approval / execution adapter; building the result sends nothing."""
    insts = engine.instruments
    inst = insts.get(instrument_id) if instrument_id is not None else (next(iter(insts.values())) if insts else None)
    policy = SafetyPolicy(cfg)
    if inst is None:
        raise ValueError("instrument not present in engine")
    now = engine.last_wall_ns if now_wall_ns is None else now_wall_ns
    f = facts_from_engine(engine, inst, now)
    return policy.evaluate(f, purpose=PURPOSE_APPROVAL, baseline=record.candidate, record=record)
