"""C9b SetupCandidate: conservative multi-timeframe continuation, fail-closed, fully explainable."""

from __future__ import annotations

import dataclasses

import pytest

from hermes.config import BarsConfig, ConfigError, DecisionConfig, config_from_mapping
from hermes.decision.candidate import (
    CandidateEngine,
    Direction,
    Regime,
    SetupCandidate,
    Vote,
    evaluate_candidate,
    orderflow_components,
)
from hermes.decision.context import build_decision_context
from hermes.decision.driver import CandidateDriver
from hermes.market.snapshot import MarketSnapshot
from tests.support import BBO, DEPTH, TICK, TRADES, Harness, RawScript, write_hrec
from tests.unit.test_replay import WEEK, ticks

S = 1_000_000_000
T0 = 1790085600                      # 2026-09-22 14:00 UTC = 09:00 CDT (RTH)
PRE_RTH = 1790078400                 # 12:00 UTC = 07:00 CDT: trading session open, RTH not yet


def shift_book(sc: RawScript, bid: float) -> None:
    """Move the whole 5-row book so that best bid = ``bid`` (asks first when moving up)."""
    for i in range(5):
        sc.depth(DEPTH, i, 1, 0, bid + TICK * (i + 1), 10 + i)
    for i in range(5):
        sc.depth(DEPTH, i, 1, 1, bid - TICK * i, 10 + i)
    sc.bbo(BBO, bid, bid + TICK)


def trend(direction: int = 1, minutes: int = 11, t0: int = T0, dip: tuple[float, int] | None = None,
          special: str = "", step_every: int = 3, contract: dict | None = None) -> RawScript:
    """Deterministic trend: the book moves ``direction`` one tick every 15 s; aggressive prints go
    with the trend (3 lots) with smaller opposite prints (1 lot). ``dip=(t, ticks)`` prints once
    against the trend to create a deep 1 m swing."""
    sc = RawScript(wall0=t0 * S)
    sc.bootstrap(**(contract or WEEK))
    bid = 21000.0
    sc.seed_book(mid_bid=bid)
    ticks(sc, 600)
    t, k = 0.0, 0
    while t < minutes * 60:
        t += 5
        sc.at(t0 + t)
        if step_every and k % step_every == 0:
            bid += direction * TICK
            shift_book(sc, bid)
            sc.advance(20)
        if dip is not None and t == dip[0]:
            sc.trade(TRADES, bid - direction * dip[1] * TICK, 1, special=special)
        if direction > 0:
            sc.trade(TRADES, bid + TICK, 3, special=special)
            if k % 2:
                sc.trade(TRADES, bid, 1, special=special)
        else:
            sc.trade(TRADES, bid, 3, special=special)
            if k % 2:
                sc.trade(TRADES, bid + TICK, 1, special=special)
        sc.tick()
        for j in range(1, 5):                     # heartbeat-like clock ticks every second
            sc.at(t0 + t + j)
            sc.tick()
        k += 1
    return sc


def run(sc: RawScript, cfg: DecisionConfig | None = None, bars: BarsConfig | None = None):
    h = Harness(bars=bars)
    ce = CandidateEngine(cfg)
    drv = CandidateDriver(h.engine, ce)
    out: list[tuple[SetupCandidate, MarketSnapshot]] = []
    for raw in sc.events:
        h.feed([raw])
        c = drv.after_event(raw, (), 0)
        if c is not None:
            out.append((c, h.engine.snapshot()))
    return h, ce, out


def first(out, direction: Direction):
    return next((c, s) for c, s in out if c.direction is direction)


@pytest.fixture(scope="module")
def long_run():
    return run(trend(+1))


@pytest.fixture(scope="module")
def short_run():
    return run(trend(-1))


# ============================================================================ LONG / SHORT

def test_long_candidate_is_fully_explained(long_run):
    h, ce, out = long_run
    c, snap = first(out, Direction.LONG)
    assert c.regime_5m.result == "LONG" and c.setup_1m.result == "LONG" and c.trigger_30s.result == "LONG"
    assert c.blocking_reasons == () and c.is_actionable_proposal and c.mode == "HUMAN_APPROVAL"
    book = snap.instruments[0].book
    assert c.entry_reference == book.asks[0][0]                          # LONG -> best ASK
    ctx = build_decision_context(snap, DecisionConfig())
    assert c.risk.structure_source == "recent_1m_window_low"
    assert c.risk.structural_level == ctx.recent_1m_window.low < book.bids[0][0]   # rolling-window extreme
    assert c.structural_invalidation == c.risk.structural_level - 2      # 2-tick buffer beyond it
    assert c.risk.required_stop_units == max(40, c.risk.structural_distance_units)
    assert c.proposed_stop == c.entry_reference - c.risk.required_stop_units
    assert c.risk_points == pytest.approx(c.risk.required_stop_units / 4) and 10 <= c.risk_points <= 12
    assert c.risk.risk_usd_per_contract == pytest.approx(c.risk_points * 2.0)
    assert c.orderflow_supporting >= 1 and c.orderflow_opposing < c.orderflow_supporting
    assert any(r.startswith("orderflow_primary:") for r in c.supporting_reasons)
    assert any("mid_above_rth_vwap" in r for r in c.supporting_reasons)
    assert 0 <= c.evaluation_lag_ms <= 2000 and c.primary_supporting >= 1
    assert c.session_rth and c.market_data_ok and all(g.passed for g in c.gates)
    assert any("MBP" in n for n in c.notes)
    assert c.trigger_bar_end_s is not None and c.trigger_bar_end_s % 30 == 0


def test_short_candidate_is_the_exact_inverse(short_run):
    h, ce, out = short_run
    c, snap = first(out, Direction.SHORT)
    book = snap.instruments[0].book
    assert c.regime_5m.result == "SHORT" and c.blocking_reasons == ()
    assert c.entry_reference == book.bids[0][0]                          # SHORT -> best BID
    assert c.structural_invalidation == c.risk.structural_level + 2
    assert c.proposed_stop == c.entry_reference + c.risk.required_stop_units
    assert any("mid_below_rth_vwap" in r for r in c.supporting_reasons)
    assert c.risk.structure_source == "recent_1m_window_high"
    assert ce.stats.long == 0 and ce.stats.short >= 1


def test_evaluates_only_once_per_completed_30s_bar(long_run):
    h, ce, out = long_run
    completed = h.engine.instruments[1].bars.completed[30]
    assert ce.stats.evaluations == len(out) == completed
    ends = [c.trigger_bar_end_s for c, _ in out]
    assert ends == sorted(set(ends))                                     # one decision point per bar


def test_regime_needs_full_completed_5m_lookback(long_run):
    h, ce, out = long_run
    early = [c for c, _ in out if c.trigger_bar_end_s - T0 < 600]
    assert early and all(c.direction is Direction.NONE for c in early)
    assert all("regime_5m_neutral" in c.blocking_reasons for c in early)


# ============================================================================ NONE

def test_flat_market_is_neutral_none():
    _, ce, out = run(trend(+1, step_every=0))                            # book never moves
    assert out and all(c.direction is Direction.NONE for c, _ in out)
    assert ce.stats.long == ce.stats.short == 0
    assert all("regime_5m_neutral" in c.blocking_reasons for c, _ in out)


def test_outside_rth_is_none_with_explicit_reason():
    _, ce, out = run(trend(+1, t0=PRE_RTH))
    assert out and all(c.direction is Direction.NONE for c, _ in out)
    c = out[-1][0]
    assert "outside_authorized_entry_hours" in c.blocking_reasons and not c.session_rth
    assert any(g.name == "authorized_entry_hours" and not g.passed for g in c.gates)


def test_unknown_calendar_fails_closed():
    _, _, out = run(trend(+1, contract=dict(trading_hours="", liquid_hours="", time_zone_id="US/Central")))
    c = out[-1][0]
    assert c.direction is Direction.NONE
    assert any(r.startswith("gate:session_calendar") for r in c.blocking_reasons)


def test_stale_book_blocks():
    sc = trend(+1)
    sc.depth(DEPTH, 9, 1, 1, 21010.0, 1)                                 # structural violation -> STALE
    sc.at((sc.wall // S) + 40)
    sc.tick()
    _, _, out = run(sc)
    c = out[-1][0]
    assert c.direction is Direction.NONE and any(r.startswith("gate:book_valid") for r in c.blocking_reasons)
    assert any(r.startswith("gate:market_data_ok") for r in c.blocking_reasons)


def test_connection_break_blocks_and_c8_continuity_is_required():
    sc = trend(+1)
    sc.error(-1, 1100)
    sc.at((sc.wall // S) + 40)
    sc.tick()
    _, _, out = run(sc)
    c = out[-1][0]
    assert c.direction is Direction.NONE
    for gate in ("connection", "c8_structure_continuity", "c8_patterns_continuity", "no_active_data_gap"):
        assert any(r.startswith(f"gate:{gate}") for r in c.blocking_reasons), gate


# ============================================================================ pure evaluation variants

@pytest.fixture(scope="module")
def long_ctx(long_run):
    _, _, out = long_run
    c, snap = first(out, Direction.LONG)
    return build_decision_context(snap, DecisionConfig()), c


def test_evaluate_is_pure_and_reproduces_the_engine_result(long_ctx):
    ctx, c = long_ctx
    again = evaluate_candidate(ctx, DecisionConfig())
    assert again == c and again == evaluate_candidate(ctx, DecisionConfig())


def test_suspect_book_blocks(long_ctx):
    ctx, _ = long_ctx
    bad = dataclasses.replace(ctx, quality=dataclasses.replace(ctx.quality, book_state="suspect",
                                                              market_data_ok=False, not_ok_reasons=("book:suspect",)))
    c = evaluate_candidate(bad)
    assert c.direction is Direction.NONE and any(r.startswith("gate:book_valid") for r in c.blocking_reasons)


def test_missing_rth_vwap_is_none_never_a_fallback(long_ctx):
    ctx, _ = long_ctx
    # full-session VWAP still available: it must NOT be used as a silent fallback
    s = dataclasses.replace(ctx.session, vs_rth_vwap_num=None, vs_rth_vwap_den=None)
    c = evaluate_candidate(dataclasses.replace(ctx, session=s))
    assert ctx.session.vs_vwap_num is not None
    assert c.direction is Direction.NONE and c.regime_5m.result == "NEUTRAL"
    assert "rth_vwap_unavailable" in c.blocking_reasons


def test_rth_vwap_is_the_primary_reference(long_ctx):
    ctx, _ = long_ctx
    below = dataclasses.replace(ctx.session, vs_rth_vwap_num=-5)           # mid below RTH VWAP
    c = evaluate_candidate(dataclasses.replace(ctx, session=below))
    assert c.direction is Direction.NONE and c.regime_5m.result == "NEUTRAL"


def test_full_session_vwap_is_caution_only(long_ctx):
    ctx, _ = long_ctx
    fs = dataclasses.replace(ctx.session, vs_vwap_num=-7)                  # mid below FULL-session VWAP
    c = evaluate_candidate(dataclasses.replace(ctx, session=fs))
    assert c.direction is Direction.LONG
    assert any(r.startswith("mid_below_full_session_vwap") for r in c.caution_reasons)


def test_stale_trigger_evaluation_is_none(long_ctx):
    ctx, c0 = long_ctx
    late = dataclasses.replace(ctx, wall_ns=c0.trigger_bar_end_s * S + 2_001_000_000)
    c = evaluate_candidate(late)
    assert c.evaluation_lag_ms == 2001 and c.direction is Direction.NONE
    assert any(r.startswith("trigger_evaluation_stale") for r in c.blocking_reasons)
    ok = evaluate_candidate(dataclasses.replace(ctx, wall_ns=c0.trigger_bar_end_s * S + 2_000_000_000))
    assert ok.evaluation_lag_ms == 2000 and ok.direction is Direction.LONG
    assert evaluate_candidate(late, DecisionConfig(max_evaluation_lag_ms=5000)).direction is Direction.LONG


def test_c8_unavailable_blocks(long_ctx):
    ctx, _ = long_ctx
    off = dataclasses.replace(ctx.structure_section, available=False, reason="book_stale")
    c = evaluate_candidate(dataclasses.replace(ctx, structure_section=off))
    assert c.direction is Direction.NONE and any("c8_structure_continuity" in r for r in c.blocking_reasons)


def _neutral_primaries(ctx):
    m = ctx.metrics
    return dataclasses.replace(ctx, metrics=dataclasses.replace(
        m, ofi=tuple(dataclasses.replace(w, value=0) for w in m.ofi),
        trade_flow=tuple(dataclasses.replace(w, buy_volume=5, sell_volume=5) for w in m.trade_flow)))


def test_no_primary_orderflow_confirmation_means_none(long_ctx):
    ctx = _neutral_primaries(long_ctx[0])
    c = evaluate_candidate(ctx)
    assert c.primary_supporting == 0 and c.direction is Direction.NONE
    assert any(r.startswith("insufficient_primary_orderflow_confirmation") for r in c.blocking_reasons)
    assert c.trigger_30s.result == "LONG"                                # the price structure alone never suffices


def test_secondary_evidence_alone_never_qualifies(long_ctx):
    ctx = _neutral_primaries(long_ctx[0])
    bm = dataclasses.replace(ctx.metrics.book, micro_offset_num=3)        # bullish microprice displacement
    ctx = dataclasses.replace(ctx, metrics=dataclasses.replace(ctx.metrics, book=bm))
    c = evaluate_candidate(ctx, DecisionConfig(orderflow_components="ofi,trade_flow,microprice"))
    micro = next(x for x in c.orderflow_evidence if x.name == "microprice")
    assert micro.role == "secondary" and micro.vote is Vote.LONG
    assert c.orderflow_supporting == 1 and c.primary_supporting == 0 and c.direction is Direction.NONE
    assert any("1 secondary only" in r for r in c.blocking_reasons)
    assert any(r.startswith("orderflow_secondary:microprice") for r in c.supporting_reasons)


def test_component_roles_are_explicit(long_ctx):
    ctx, _ = long_ctx
    roles = {c.name: c.role for c in orderflow_components(ctx, DecisionConfig())}
    assert roles == {"ofi": "primary", "trade_flow": "primary", "sweep_follow": "primary",
                     "microprice": "secondary", "absorption_compatible": "secondary"}


def test_conflicting_orderflow_is_none_with_reasons(long_ctx):
    ctx, _ = long_ctx
    m = ctx.metrics
    ofi = tuple(dataclasses.replace(w, value=-abs(w.value) - 1) for w in m.ofi)
    mixed = dataclasses.replace(ctx, metrics=dataclasses.replace(m, ofi=ofi))
    cfg = DecisionConfig(orderflow_components="ofi,trade_flow")
    c = evaluate_candidate(mixed, cfg)
    assert c.orderflow_supporting == 1 and c.orderflow_opposing == 1
    assert c.direction is Direction.NONE
    assert any(r.startswith("conflicting_orderflow_evidence") for r in c.blocking_reasons)
    assert any(r.startswith("orderflow_opposes_primary:ofi") for r in c.caution_reasons)


def test_unknown_volume_never_votes(long_ctx):
    ctx, _ = long_ctx
    m = ctx.metrics
    tf = tuple(dataclasses.replace(w, buy_volume=0, sell_volume=0, unknown_volume=500) for w in m.trade_flow)
    unk = dataclasses.replace(ctx, metrics=dataclasses.replace(m, trade_flow=tf))
    comps = {x.name: x for x in orderflow_components(unk, DecisionConfig())}
    assert comps["trade_flow"].vote is Vote.NEUTRAL and "unknown=500" in comps["trade_flow"].detail
    c = evaluate_candidate(unk, DecisionConfig(orderflow_components="trade_flow"))
    assert c.direction is Direction.NONE


def test_all_unknown_tape_cannot_create_direction():
    # every print is classifier-ineligible (UNKNOWN) but bar-eligible: volume exists, known delta is 0
    _, ce, out = run(trend(+1, special="Z"), bars=BarsConfig(allowed_special_conditions="Z"))
    assert out and ce.stats.long == ce.stats.short == 0
    late = [c for c, _ in out if c.trigger_bar_end_s - T0 >= 600]
    assert late and all("5m_known_delta" in " ".join(c.blocking_reasons) for c in late)


# ============================================================================ stop / invalidation

def _with_swing_low(ctx, low: int):
    """Force recent_1m_window_low (rolling-window extreme of the last 3 completed 1 m bars)."""
    return dataclasses.replace(ctx, recent_1m_window=dataclasses.replace(ctx.recent_1m_window, low=low))


def test_structural_stop_within_band_uses_structure(long_ctx):
    ctx, _ = long_ctx
    entry = ctx.price.best_ask
    c = evaluate_candidate(_with_swing_low(ctx, entry - 42))            # 42 + 2 buffer = 44 units = 11 pt
    assert c.direction is Direction.LONG
    assert c.risk.structural_distance_units == 44 and c.risk.required_stop_units == 44
    assert c.risk_points == 11.0 and c.proposed_stop == c.structural_invalidation == entry - 44


def test_tight_structure_still_gets_the_minimum_stop(long_ctx):
    ctx, _ = long_ctx
    entry = ctx.price.best_ask
    c = evaluate_candidate(_with_swing_low(ctx, entry - 4))
    assert c.direction is Direction.LONG and c.risk_points == 10.0
    assert c.proposed_stop == entry - 40 < c.structural_invalidation     # beyond structure, never inside


def test_exactly_twelve_points_is_allowed(long_ctx):
    ctx, _ = long_ctx
    entry = ctx.price.best_ask
    c = evaluate_candidate(_with_swing_low(ctx, entry - 46))            # 48 units = 12 pt
    assert c.direction is Direction.LONG and c.risk_points == 12.0


def test_structure_beyond_twelve_points_is_none_never_a_capped_stop(long_ctx):
    ctx, _ = long_ctx
    entry = ctx.price.best_ask
    c = evaluate_candidate(_with_swing_low(ctx, entry - 54))            # 56 units = 14 pt
    assert c.direction is Direction.NONE and c.proposed_stop is None and c.entry_reference is None
    assert c.risk.required_stop_units == 56 and not c.risk.within_max_risk
    assert any(r.startswith("structural stop exceeds maximum risk") for r in c.blocking_reasons)


def test_deep_swing_in_real_flow_blocks():
    # one print 60 ticks against the trend inside the swing window -> structure needs > 12 pt
    sc = trend(+1, dip=(560.0, 60))
    _, ce, out = run(sc)
    after = [c for c, _ in out if 570 <= c.trigger_bar_end_s - T0 <= 690]
    assert any(any(r.startswith("structural stop exceeds maximum risk") for r in c.blocking_reasons) for c in after)
    assert all(c.direction is Direction.NONE or c.risk_points <= 12 for c, _ in out)


# ============================================================================ determinism / replay

def test_replay_reproduces_candidates_exactly(tmp_path):
    from hermes.replay.runner import ReplayOptions, replay_session
    sc = trend(+1)
    d = write_hrec(tmp_path / "s", sc.events)
    opts = ReplayOptions(observers=(lambda eng: CandidateDriver(eng, CandidateEngine()),))
    r1, r2 = replay_session(d, opts), replay_session(d, opts)
    c1, c2 = r1.observers[0].candidates, r2.observers[0].candidates
    _, direct, _ = run(sc)
    assert c1.fingerprint() == c2.fingerprint() == direct.fingerprint()
    assert tuple(c1.history) == tuple(direct.history) and c1.stats.long >= 1
    assert r1.final_hash == r2.final_hash                               # market hash untouched by decisions


def test_decision_layer_does_not_change_market_state_hash(tmp_path):
    from hermes.replay.runner import ReplayOptions, replay_session
    d = write_hrec(tmp_path / "s", trend(+1).events)
    with_dec = replay_session(d, ReplayOptions(observers=(lambda eng: CandidateDriver(eng, CandidateEngine()),)))
    without = replay_session(d)
    assert with_dec.final_hash == without.final_hash
    assert [c.hash for c in with_dec.checkpoints] == [c.hash for c in without.checkpoints]


# ============================================================================ config / safety

def test_decision_config_validation():
    for bad in ({"entry_hours": "ALWAYS"}, {"orderflow_components": "ofi,magic"}, {"orderflow_components": ""},
                {"orderflow_window_s": 7}, {"min_primary_confirmations": 0}, {"min_stop_points": 13.0},
                {"orderflow_components": "microprice,absorption_compatible"},   # no primary component
                {"max_evaluation_lag_ms": -1},
                {"stop_buffer_ticks": -1}, {"regime_bars_5m": 11}):
        with pytest.raises(ConfigError):
            config_from_mapping({"decision": bad})
    d = config_from_mapping({}).decision
    assert (d.regime_bars_5m, d.setup_bars_1m, d.stop_buffer_ticks, d.min_stop_points, d.max_stop_points,
            d.entry_hours) == (2, 3, 2, 10.0, 12.0, "RTH_ONLY")
    assert (d.min_primary_confirmations, d.max_evaluation_lag_ms, d.recent_window_bars_1m) == (1, 2000, 3)


def test_candidate_is_a_proposal_object_only(long_ctx):
    _, c = long_ctx
    names = {n for n in dir(c) if not n.startswith("_")}
    assert not names & {"submit", "place", "send", "execute", "order", "place_order"}
    assert Regime.NEUTRAL.value == "NEUTRAL" and c.external_evidence == ()
