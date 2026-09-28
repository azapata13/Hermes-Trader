"""C9b — SetupCandidate: conservative multi-timeframe CONTINUATION setup (proposal only).

Hierarchy (never collapsed into one opaque score):

    5 m  REGIME   completed bars: quality OK, valid RTH session, mid vs RTH VWAP (primary),
                  aggregate net change and known delta all agree        -> LONG | SHORT | NEUTRAL
    1 m  SETUP    agrees with the regime: quality OK, net change, known delta, latest bar
                  closes in the direction                               -> LONG | SHORT | NONE
    30 s TRIGGER  latest COMPLETED 30 s bar: quality OK, closes in the direction, known delta
                  in the direction, plus >= N PRIMARY order-flow confirmations (C7 OFI, known
                  trade-flow dominance, sweep with favorable follow-through); SECONDARY evidence
                  (microprice, absorption-compatible caps) only supports or cautions
    LAG           evaluated at most max_evaluation_lag_ms (event time) after the 30 s bar end
    RISK          entry = best ASK (LONG) / best BID (SHORT) of a VALID book; structural
                  invalidation = recent_1m_window_low/_high (rolling extreme of the last N completed
                  1 m bars, NOT a pivot) -/+ buffer; required stop = max(min_stop, structural
                  distance); > max_stop => NONE (never a capped stop)

Order flow confirms; it can never create a candidate by itself. UNKNOWN aggressor volume never
counts as directional evidence. Any failed hard gate => NONE (fail closed). Mixed order flow
(opposing directional votes >= supporting directional votes, primary + secondary) => NONE with
explicit conflict reasons.

``evaluate_candidate`` is a pure function of a DecisionContext + config. ``CandidateEngine`` is a
deterministic, clock-free layer OUTSIDE MarketEngine that evaluates exactly once per event on
which a 30 s bar completed, keeps a bounded history and has its own fingerprint. Nothing here can
place, modify or cancel an order: the output is a proposal for the future HUMAN_APPROVAL workflow.

Future extension points (not implemented in C9): historical setup memory, shadow variants,
learned-rule candidates, post-trade critique, experiment telemetry. They may ADD evidence
(``external_evidence``) but can never remove a blocking reason or turn NONE into a direction.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from hermes.config import PRIMARY_ORDERFLOW, DecisionConfig
from hermes.decision.context import EPISTEMIC_NOTES, BarContext, DecisionContext, build_decision_context
from hermes.market.bars import Bar, BarFlag
from hermes.market.snapshot import MarketSnapshot

CANDIDATE_SCHEMA_VERSION = 1

_BAD_FLAGS = int(BarFlag.DATA_GAP | BarFlag.MARKET_DATA_INVALID | BarFlag.CONNECTION_INTERRUPTION | BarFlag.PARTIAL)
_CAUTION_FLAGS = int(BarFlag.LATE_DATA_OBSERVED | BarFlag.SESSION_BOUNDARY)


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class Regime(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class Vote(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class Condition:
    name: str
    status: str                        # "pass" | "fail" | "unavailable"
    detail: str

    @property
    def passed(self) -> bool:
        return self.status == "pass"


@dataclass(frozen=True, slots=True)
class OrderFlowComponent:
    """One explicit, individually testable order-flow evidence item (never summed into a score)."""
    name: str
    role: str                          # "primary" (can confirm) | "secondary" (supporting/caution only)
    vote: Vote
    window_s: int
    detail: str                        # the exact integer inputs behind the vote


@dataclass(frozen=True, slots=True)
class Assessment:
    stage: str                         # "regime_5m" | "setup_1m" | "trigger_30s"
    result: str                        # LONG | SHORT | NEUTRAL/NONE
    conditions: tuple[Condition, ...]
    bar_context: BarContext | None     # evidence the stage looked at


@dataclass(frozen=True, slots=True)
class RiskProposal:
    entry_reference: int               # units: best ask (LONG) / best bid (SHORT) of a VALID book
    entry_side: str                    # "best_ask" | "best_bid"
    structure_source: str              # "recent_1m_window_low" (LONG) | "recent_1m_window_high" (SHORT)
    structural_level: int              # that rolling-window extreme (NOT a formally detected pivot)
    structural_invalidation: int       # structural level -/+ buffer
    structural_distance_units: int     # entry -> invalidation
    required_stop_units: int           # max(min_stop, structural distance)
    proposed_stop: int                 # entry -/+ required stop (never tighter than structure)
    risk_points: float
    risk_usd_per_contract: float
    min_stop_units: int
    max_stop_units: int
    buffer_units: int
    within_max_risk: bool


@dataclass(frozen=True, slots=True)
class SetupCandidate:
    schema_version: int
    direction: Direction
    instrument_id: int
    symbol: str
    seq: int                           # deterministic event position of the evaluation
    mono_ns: int                       # recorded event time
    wall_ns: int
    trigger_bar_end_s: int | None      # end of the completed 30 s bar that triggered the evaluation
    evaluation_lag_ms: int | None      # event time (wall_ns) - trigger bar end; never hidden
    regime_5m: Assessment
    setup_1m: Assessment
    trigger_30s: Assessment
    orderflow_evidence: tuple[OrderFlowComponent, ...]
    orderflow_supporting: int          # directional votes WITH the direction (primary + secondary)
    orderflow_opposing: int            # directional votes AGAINST it (primary + secondary)
    primary_supporting: int            # the confirmation requirement counts these only
    entry_reference: int | None
    structural_invalidation: int | None
    proposed_stop: int | None
    risk_points: float | None
    risk: RiskProposal | None
    supporting_reasons: tuple[str, ...]
    caution_reasons: tuple[str, ...]
    blocking_reasons: tuple[str, ...]
    gates: tuple[Condition, ...]       # market-data / session / continuity gates (fail closed)
    continuity: tuple[tuple[str, int | None], ...]
    stream_generations: tuple[tuple[str, int | None], ...]
    session_rth: bool
    market_data_ok: bool
    mode: str = "HUMAN_APPROVAL"       # proposal only; there is no order path
    notes: tuple[str, ...] = EPISTEMIC_NOTES
    external_evidence: tuple[str, ...] = ()   # future memory/shadow/LLM evidence: informational only

    @property
    def is_actionable_proposal(self) -> bool:
        return self.direction is not Direction.NONE and not self.blocking_reasons


# ---------------------------------------------------------------------------- helpers

def _cond(name: str, ok: bool | None, detail: str) -> Condition:
    return Condition(name, "unavailable" if ok is None else ("pass" if ok else "fail"), detail)


def _sign_ok(value: int | None, want: Regime) -> bool | None:
    if value is None:
        return None
    return value > 0 if want is Regime.LONG else value < 0


def _bar_quality(bc: BarContext, need: int) -> tuple[bool | None, str]:
    if not bc.available:
        return None, bc.reason
    if bc.bars_used < need:
        return False, f"only {bc.bars_used}/{need} completed bars"
    if bc.traded_bars < need:
        return False, f"{bc.empty_bars} empty bar(s) in lookback"
    bad = bc.flags & _BAD_FLAGS
    if bad:
        return False, f"flags={BarFlag(bad).name}"
    return True, "ok"


def _latest_bar_direction(bar: Bar | None, want: Regime) -> tuple[bool | None, str]:
    if bar is None or bar.trades == 0:
        return None, "no traded latest bar"
    ok = bar.close > bar.open if want is Regime.LONG else bar.close < bar.open
    return ok, f"open={bar.open} close={bar.close}"


def _points_to_units(points: float, upp: int) -> int | None:
    u = points * upp
    r = round(u)
    return r if abs(u - r) < 1e-9 else None


# ---------------------------------------------------------------------------- gates

def gates(ctx: DecisionContext, cfg: DecisionConfig) -> tuple[Condition, ...]:
    q, p, s = ctx.quality, ctx.price, ctx.session
    out = [
        _cond("connection", q.connection == "connected", q.connection),
        _cond("contract_defined", q.contract_state == "defined", q.contract_state),
        _cond("live_market_data", q.market_data_type == 1 and not q.not_live,
              f"market_data_type={q.market_data_type} not_live={q.not_live}"),
        _cond("no_session_conflict", q.conflict_phase == "none", q.conflict_phase),
        _cond("no_critical_alert", not q.alerts, ",".join(q.alerts) or "none"),
        _cond("market_data_ok", q.market_data_ok, ",".join(q.not_ok_reasons) or "ok"),
        _cond("book_valid", q.book_state == "valid", str(q.book_state)),
        _cond("no_active_data_gap", q.bar_active_flags == 0,
              BarFlag(q.bar_active_flags).name if q.bar_active_flags else "none"),
        _cond("classification_context", q.classification_context_ok, q.classification_context_reason),
        _cond("price_available", p.available and p.best_bid is not None and p.best_ask is not None, p.reason),
        _cond("price_grid_uniform", p.units_per_point is not None and p.tick_units is not None,
              f"units_per_point={p.units_per_point} tick_units={p.tick_units}"),
        _cond("session_calendar", q.session_calendar_ok and s.available, s.reason),
        _cond("c8_structure_continuity", ctx.structure_section.available, ctx.structure_section.reason),
        _cond("c8_patterns_continuity", ctx.patterns_section.available, ctx.patterns_section.reason),
        _cond("c7_metrics_available", ctx.flow.available, ctx.flow.reason),
    ]
    if cfg.entry_hours == "RTH_ONLY":
        out.append(_cond("authorized_entry_hours", s.available and s.in_rth,
                         "in_rth" if s.in_rth else "outside_authorized_entry_hours"))
    return tuple(out)


# ---------------------------------------------------------------------------- stages

def assess_regime(ctx: DecisionContext, cfg: DecisionConfig) -> Assessment:
    bc = ctx.regime_5m
    s = ctx.session
    qok, qd = _bar_quality(bc, cfg.regime_bars_5m)
    session_ok = s.available and s.in_trading_session and s.in_rth
    vwap = s.vs_rth_vwap_num if s.vs_rth_vwap_den else None      # RTH VWAP: primary reference for RTH entries
    results = {}
    for want in (Regime.LONG, Regime.SHORT):
        results[want] = (
            _cond("5m_quality", qok, qd),
            _cond("session_rth_valid", session_ok, s.reason + (" rth" if s.in_rth else " not_rth")),
            _cond(f"mid_{'above' if want is Regime.LONG else 'below'}_rth_vwap", _sign_ok(vwap, want),
                  "rth_vwap_unavailable" if vwap is None
                  else f"mid-rth_vwap={s.vs_rth_vwap_num}/{s.vs_rth_vwap_den} units"),
            _cond("5m_net_change", _sign_ok(bc.net_change_units, want), f"net_change_units={bc.net_change_units}"),
            _cond("5m_known_delta", _sign_ok(bc.known_delta if bc.available else None, want),
                  f"known_delta={bc.known_delta} unknown_volume={bc.unknown_volume}"),
        )
    for want in (Regime.LONG, Regime.SHORT):
        if all(c.passed for c in results[want]):
            return Assessment("regime_5m", want.value, results[want], bc)
    # NEUTRAL: report the side that got closer (more passed conditions; LONG on ties) for explainability
    lp = sum(c.passed for c in results[Regime.LONG])
    sp = sum(c.passed for c in results[Regime.SHORT])
    shown = results[Regime.LONG] if lp >= sp else results[Regime.SHORT]
    return Assessment("regime_5m", Regime.NEUTRAL.value, shown, bc)


def assess_setup(ctx: DecisionContext, cfg: DecisionConfig, regime: str) -> Assessment:
    bc = ctx.setup_1m
    if regime == Regime.NEUTRAL.value:
        return Assessment("setup_1m", Direction.NONE.value,
                          (_cond("agrees_with_5m_regime", False, "regime NEUTRAL"),), bc)
    want = Regime(regime)
    qok, qd = _bar_quality(bc, cfg.setup_bars_1m)
    lb, lbd = _latest_bar_direction(bc.latest, want)
    conds = (
        _cond("agrees_with_5m_regime", True, regime),
        _cond("1m_quality", qok, qd),
        _cond("1m_net_change", _sign_ok(bc.net_change_units, want), f"net_change_units={bc.net_change_units}"),
        _cond("1m_known_delta", _sign_ok(bc.known_delta if bc.available else None, want),
              f"known_delta={bc.known_delta} unknown_volume={bc.unknown_volume}"),
        _cond("1m_latest_bar_direction", lb, lbd),
    )
    return Assessment("setup_1m", want.value if all(c.passed for c in conds) else Direction.NONE.value, conds, bc)


def assess_trigger(ctx: DecisionContext, cfg: DecisionConfig, setup: str) -> Assessment:
    bc = ctx.bars_30s
    bar = bc.latest
    if setup == Direction.NONE.value:
        return Assessment("trigger_30s", Direction.NONE.value,
                          (_cond("agrees_with_1m_setup", False, "setup NONE"),), bc)
    want = Regime(setup)
    if bar is None:
        return Assessment("trigger_30s", Direction.NONE.value,
                          (_cond("30s_bar_available", None, bc.reason),), bc)
    bad = int(bar.flags) & _BAD_FLAGS
    lb, lbd = _latest_bar_direction(bar, want)
    conds = (
        _cond("agrees_with_1m_setup", True, setup),
        _cond("30s_quality", bar.trades > 0 and not bad,
              "no prints" if bar.trades == 0 else (f"flags={BarFlag(bad).name}" if bad else "ok")),
        _cond("30s_bar_direction", lb, lbd),
        _cond("30s_known_delta", _sign_ok(bar.known_delta if bar.trades else None, want),
              f"known_delta={bar.known_delta} unknown_volume={bar.unknown_volume}"),
    )
    return Assessment("trigger_30s", want.value if all(c.passed for c in conds) else Direction.NONE.value, conds, bc)


# ---------------------------------------------------------------------------- order flow

def _by_seconds(items, seconds: int):
    for x in items or ():
        if x.seconds == seconds:
            return x
    return None


def _role(name: str) -> str:
    return "primary" if name in PRIMARY_ORDERFLOW else "secondary"


def orderflow_components(ctx: DecisionContext, cfg: DecisionConfig) -> tuple[OrderFlowComponent, ...]:
    w = cfg.orderflow_window_s
    wanted = [c.strip() for c in cfg.orderflow_components.split(",") if c.strip()]
    m = ctx.metrics
    out = []
    for name in wanted:
        if name == "ofi":
            ow = _by_seconds(m.ofi, w) if m is not None and ctx.flow.available else None
            if ow is None:
                out.append(OrderFlowComponent(name, _role(name), Vote.UNAVAILABLE, w, ctx.flow.reason))
            else:
                v = Vote.LONG if ow.value > 0 else Vote.SHORT if ow.value < 0 else Vote.NEUTRAL
                out.append(OrderFlowComponent(name, _role(name), v, w, f"ofi={ow.value} events={ow.events}"))
        elif name == "trade_flow":
            tf = _by_seconds(m.trade_flow, w) if m is not None else None
            if tf is None:
                out.append(OrderFlowComponent(name, _role(name), Vote.UNAVAILABLE, w, "no metrics"))
            else:
                # UNKNOWN volume is reported but never votes
                v = (Vote.LONG if tf.buy_volume > tf.sell_volume else Vote.SHORT if tf.sell_volume > tf.buy_volume
                     else Vote.NEUTRAL)
                out.append(OrderFlowComponent(name, _role(name), v, w, f"buy={tf.buy_volume} sell={tf.sell_volume} "
                                                         f"unknown={tf.unknown_volume} (unknown never votes)"))
        elif name == "microprice":
            bm = m.book if m is not None else None
            if bm is None or not bm.available or bm.micro_offset_num is None:
                out.append(OrderFlowComponent(name, _role(name), Vote.UNAVAILABLE, 0, bm.reason if bm else "no metrics"))
            else:
                n = bm.micro_offset_num
                v = Vote.LONG if n > 0 else Vote.SHORT if n < 0 else Vote.NEUTRAL
                out.append(OrderFlowComponent(name, _role(name), v, 0, f"micro-mid={n}/{bm.micro_offset_den} units (L1 sizes)"))
        elif name == "sweep_follow":
            pw = _by_seconds(ctx.patterns.windows, w) if ctx.patterns is not None and ctx.patterns_section.available else None
            if pw is None:
                out.append(OrderFlowComponent(name, _role(name), Vote.UNAVAILABLE, w, ctx.patterns_section.reason))
            else:
                buy = pw.buy_sweeps > 0 and pw.buy_follow_volume > pw.buy_no_follow_volume
                sell = pw.sell_sweeps > 0 and pw.sell_follow_volume > pw.sell_no_follow_volume
                v = Vote.LONG if buy and not sell else Vote.SHORT if sell and not buy else Vote.NEUTRAL
                out.append(OrderFlowComponent(name, _role(name), v, w, (
                    f"buy_sweeps={pw.buy_sweeps} buy_follow={pw.buy_follow_volume}/no={pw.buy_no_follow_volume} "
                    f"sell_sweeps={pw.sell_sweeps} sell_follow={pw.sell_follow_volume}/no={pw.sell_no_follow_volume}")))
        elif name == "absorption_compatible":
            ab = ctx.absorption
            aw = _by_seconds(ab.windows, w) if ab is not None and ab.available else None
            if aw is None:
                out.append(OrderFlowComponent(name, _role(name), Vote.UNAVAILABLE, w, ab.reason if ab else "not_present"))
            else:
                # resting BUYERS compatible with sell aggression into the bid -> LONG context (and inverse)
                bid_cap = aw.sell_vs_bid.compatible_cap_volume
                ask_cap = aw.buy_vs_ask.compatible_cap_volume
                v = (Vote.LONG if bid_cap > 0 and ask_cap == 0 else Vote.SHORT if ask_cap > 0 and bid_cap == 0
                     else Vote.NEUTRAL)
                out.append(OrderFlowComponent(name, _role(name), v, w, (
                    f"sell_vs_bid cap={bid_cap} buy_vs_ask cap={ask_cap} "
                    "(aggregate upper bounds, MBP: compatible with, never proof of, absorption)")))
    return tuple(out)


# ---------------------------------------------------------------------------- risk

def propose_risk(ctx: DecisionContext, cfg: DecisionConfig, direction: Regime) -> tuple[RiskProposal | None, str]:
    p = ctx.price
    upp, tick = p.units_per_point, p.tick_units
    if upp is None or tick is None:
        return None, "price_grid_not_uniform"
    if not p.available or p.best_ask is None or p.best_bid is None:
        return None, "no_valid_book_for_entry_reference"
    sw = ctx.recent_1m_window
    if not sw.available or sw.low is None or sw.high is None:
        return None, f"no_recent_1m_window:{sw.reason}"
    qok, qd = _bar_quality(sw, cfg.recent_window_bars_1m)
    if not qok:
        return None, f"recent_1m_window_quality:{qd}"
    min_u, max_u = _points_to_units(cfg.min_stop_points, upp), _points_to_units(cfg.max_stop_points, upp)
    if min_u is None or max_u is None:
        return None, "risk_band_not_on_grid"
    buf = cfg.stop_buffer_ticks * tick
    if direction is Regime.LONG:
        entry, side, source, level = p.best_ask, "best_ask", "recent_1m_window_low", sw.low
        inval = level - buf
        dist = entry - inval
    else:
        entry, side, source, level = p.best_bid, "best_bid", "recent_1m_window_high", sw.high
        inval = level + buf
        dist = inval - entry
    if dist <= 0:
        return None, "structure_not_beyond_entry"
    required = max(min_u, dist)
    stop = entry - required if direction is Regime.LONG else entry + required
    pts = required / upp
    return RiskProposal(entry, side, source, level, inval, dist, required, stop, pts, pts * cfg.point_value_usd,
                        min_u, max_u, buf, required <= max_u), "ok"


# ---------------------------------------------------------------------------- evaluation

def evaluate_candidate(ctx: DecisionContext, cfg: DecisionConfig | None = None) -> SetupCandidate:
    """Pure: one candidate (LONG | SHORT | NONE) from one DecisionContext. The trigger bar is the
    latest COMPLETED 30 s bar of the context; the evaluation time is the context's event time."""
    cfg = cfg or DecisionConfig()
    g = gates(ctx, cfg)
    regime = assess_regime(ctx, cfg)
    setup = assess_setup(ctx, cfg, regime.result)
    trigger = assess_trigger(ctx, cfg, setup.result)
    flow = orderflow_components(ctx, cfg)

    supporting: list[str] = []
    caution: list[str] = []
    blocking: list[str] = [f"gate:{c.name}:{c.detail}" if c.name != "authorized_entry_hours"
                           else "outside_authorized_entry_hours" for c in g if not c.passed]

    # evaluation lag (event time, deterministic in replay): never hidden, fail closed when stale
    tbar = ctx.bars_30s.latest
    trigger_end = tbar.end_s if tbar is not None else None
    lag_ms = None if trigger_end is None else (ctx.wall_ns - trigger_end * 1_000_000_000) // 1_000_000
    if lag_ms is None:
        blocking.append("no_completed_30s_trigger_bar")
    elif lag_ms > cfg.max_evaluation_lag_ms:
        blocking.append(f"trigger_evaluation_stale:{lag_ms}ms>{cfg.max_evaluation_lag_ms}ms")

    for a in (regime, setup, trigger):
        for c in a.conditions:
            if c.passed:
                supporting.append(f"{a.stage}:{c.name}:{c.detail}")
    if regime.result == Regime.NEUTRAL.value:
        blocking.append("regime_5m_neutral")
        if any(c.detail == "rth_vwap_unavailable" for c in regime.conditions):
            blocking.append("rth_vwap_unavailable")
    elif setup.result == Direction.NONE.value:
        blocking.append("setup_1m_not_confirmed")
    elif trigger.result == Direction.NONE.value:
        blocking.append("trigger_30s_not_confirmed")
    for a in (regime, setup, trigger):
        for c in a.conditions:
            if not c.passed and a.result in (Regime.NEUTRAL.value, Direction.NONE.value):
                blocking.append(f"{a.stage}:{c.name}:{c.status}:{c.detail}")

    # bar-quality cautions (non-blocking flags)
    for bc, label in ((ctx.regime_5m, "5m"), (ctx.setup_1m, "1m"), (ctx.bars_30s, "30s")):
        f = bc.flags & _CAUTION_FLAGS
        if f:
            caution.append(f"{label}_bars_flagged:{BarFlag(f).name}")
    if tbar is not None and tbar.unknown_volume > tbar.buy_volume + tbar.sell_volume:
        caution.append("30s_trigger_bar_mostly_unknown_aggressor")

    direction = Direction.NONE
    sup = opp = psup = 0
    risk = None
    want = Regime(trigger.result) if trigger.result in (Regime.LONG.value, Regime.SHORT.value) else None
    if want is not None:
        # full-session VWAP (incl. overnight) is context/caution only for RTH entries
        fs = ctx.session.vs_vwap_num if ctx.session.vs_vwap_den else None
        if fs is not None and _sign_ok(fs, want) is False:
            caution.append(f"mid_{'below' if want is Regime.LONG else 'above'}_full_session_vwap:"
                           f"{ctx.session.vs_vwap_num}/{ctx.session.vs_vwap_den} units")
        against = Vote.SHORT if want is Regime.LONG else Vote.LONG
        for c in flow:
            if c.vote.value == want.value:
                sup += 1
                psup += c.role == "primary"
                supporting.append(f"orderflow_{c.role}:{c.name}:{c.detail}")
            elif c.vote is against:
                opp += 1
                caution.append(f"orderflow_opposes_{c.role}:{c.name}:{c.detail}")
            elif c.vote is Vote.UNAVAILABLE:
                caution.append(f"orderflow_unavailable:{c.name}:{c.detail}")
        if psup < cfg.min_primary_confirmations:
            blocking.append(f"insufficient_primary_orderflow_confirmation:{psup}/{cfg.min_primary_confirmations}"
                            + (f" ({sup - psup} secondary only)" if sup > psup else ""))
        if opp and opp >= sup:
            blocking.append(f"conflicting_orderflow_evidence:{sup}_supporting_vs_{opp}_opposing")
        risk, why = propose_risk(ctx, cfg, want)
        if risk is None:
            blocking.append(f"risk:{why}")
        elif not risk.within_max_risk:
            blocking.append(f"structural stop exceeds maximum risk: {risk.required_stop_units / ctx.price.units_per_point:g} "  # type: ignore[operator]
                            f"pt > {cfg.max_stop_points:g} pt")
        if not blocking:
            direction = Direction(want.value)

    q = ctx.quality
    act = direction is not Direction.NONE
    return SetupCandidate(
        schema_version=CANDIDATE_SCHEMA_VERSION, direction=direction, instrument_id=ctx.instrument_id,
        symbol=ctx.symbol, seq=ctx.seq, mono_ns=ctx.mono_ns, wall_ns=ctx.wall_ns,
        trigger_bar_end_s=trigger_end, evaluation_lag_ms=lag_ms,
        regime_5m=regime, setup_1m=setup, trigger_30s=trigger,
        orderflow_evidence=flow, orderflow_supporting=sup, orderflow_opposing=opp, primary_supporting=psup,
        entry_reference=risk.entry_reference if risk and act else None,
        structural_invalidation=risk.structural_invalidation if risk and act else None,
        proposed_stop=risk.proposed_stop if risk and act else None,
        risk_points=risk.risk_points if risk and act else None,
        risk=risk,
        supporting_reasons=tuple(supporting), caution_reasons=tuple(caution),
        blocking_reasons=tuple(dict.fromkeys(blocking)),
        gates=g,
        continuity=(("tape_epoch", q.tape_epoch), ("classifier_epoch", q.classifier_epoch),
                    ("metrics_epoch", q.metrics_epoch), ("structure_epoch", q.structure_epoch),
                    ("pattern_epoch", q.pattern_epoch), ("book_epoch", q.book_epoch)),
        stream_generations=tuple((name, gen) for name, gen, _ in q.streams),
        session_rth=ctx.session.in_rth, market_data_ok=q.market_data_ok, mode=cfg.mode,
    )


# ---------------------------------------------------------------------------- engine

@dataclass(slots=True)
class CandidateStats:
    evaluations: int = 0
    long: int = 0
    short: int = 0
    none: int = 0
    skipped_no_instrument: int = 0
    blocking_counts: dict[str, int] = field(default_factory=dict)


class CandidateEngine:
    """Deterministic decision layer OUTSIDE MarketEngine. Evaluates once per event on which the
    number of completed 30 s bars increased (the latest completed bar is the trigger bar)."""

    def __init__(self, cfg: DecisionConfig | None = None, instrument_id: int | None = None) -> None:
        self.cfg = cfg or DecisionConfig()
        self.instrument_id = instrument_id
        self.history: deque[SetupCandidate] = deque(maxlen=self.cfg.candidate_history)
        self.stats = CandidateStats()
        self._last_completed = 0
        self._chain = "0" * 64                  # rolling SHA-256 over every evaluation ever recorded

    def due(self, completed_30s: int) -> bool:
        return completed_30s > self._last_completed

    def on_snapshot(self, snap: MarketSnapshot) -> SetupCandidate | None:
        """Evaluate iff at least one new 30 s bar completed since the last evaluation."""
        inst = snap.instruments[0] if snap.instruments and self.instrument_id is None else (
            snap.instrument(self.instrument_id) if self.instrument_id is not None else None)
        if inst is None or inst.bars is None:
            self.stats.skipped_no_instrument += 1
            return None
        n = inst.bars.completed_30s
        if not self.due(n):
            return None
        self._last_completed = n
        ctx = build_decision_context(snap, self.cfg, inst.instrument_id)
        if ctx is None:
            return None
        cand = evaluate_candidate(ctx, self.cfg)
        self._record(cand)
        return cand

    def _record(self, c: SetupCandidate) -> None:
        s = self.stats
        s.evaluations += 1
        if c.direction is Direction.LONG:
            s.long += 1
        elif c.direction is Direction.SHORT:
            s.short += 1
        else:
            s.none += 1
        for r in c.blocking_reasons:
            parts = r.split(":")
            key = ":".join(parts[:2]) if parts[0] in ("gate", "risk", "regime_5m", "setup_1m", "trigger_30s") else parts[0]
            s.blocking_counts[key] = s.blocking_counts.get(key, 0) + 1
        self.history.append(c)
        from hermes.replay.fingerprint import digest
        self._chain = digest((self._chain, c))  # O(1) per evaluation; covers evicted history too

    def fingerprint(self) -> str:
        """Deterministic digest of the decision layer's state (independent of the market hash).
        Cheap: the evaluation history enters through the rolling chain digest."""
        from hermes.replay.fingerprint import digest
        return digest(("hermes-decision", CANDIDATE_SCHEMA_VERSION, self.cfg, self._last_completed,
                       self.stats.evaluations, self.stats.long, self.stats.short, self.stats.none,
                       self._chain))
