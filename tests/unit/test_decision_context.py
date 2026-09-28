"""C9a DecisionContext: pure, deterministic evidence with explicit availability (no signals)."""

from __future__ import annotations

import dataclasses

import pytest

from hermes.config import BarsConfig, ConfigError, DecisionConfig, config_from_mapping
from hermes.decision.context import (
    EPISTEMIC_NOTES,
    DecisionContext,
    bar_context,
    build_decision_context,
    context_digest,
)
from hermes.market.bars import BarFlag
from tests.support import BASE, DEPTH, TICK, TRADES, Harness, RawScript, write_hrec
from tests.unit.test_replay import T0, WEEK, ready, ticks

S = 1_000_000_000


def traded_script(n=40, step_s=9.0, special=""):
    sc = ready()
    for i in range(n):
        sc.at(T0 + 1 + i * step_s)
        sc.trade(TRADES, BASE + TICK * (i % 3), 1 + i % 2, special=special)
        sc.tick()
    return sc


def ctx_of(sc, bars=None, cfg=None) -> DecisionContext:
    h = Harness(bars=bars).run(sc)
    return build_decision_context(h.engine.snapshot(), cfg)


# ---------------------------------------------------------------------------- determinism

def test_context_is_pure_and_deterministic():
    sc = traded_script()
    h = Harness().run(sc)
    snap = h.engine.snapshot()
    a, b = build_decision_context(snap), build_decision_context(snap)
    assert a == b and context_digest(a) == context_digest(b)
    c = ctx_of(sc)                                            # an independent engine, same raw stream
    assert c == a and context_digest(c) == context_digest(a)
    assert a.seq == snap.seq and a.mono_ns == snap.mono_ns and a.wall_ns == snap.wall_ns


def test_context_from_replayed_recording_equals_direct_processing(tmp_path):
    from hermes.replay.runner import replay_session
    sc = traded_script()
    r = replay_session(write_hrec(tmp_path / "s", sc.events))
    assert r.integrity.replay_complete
    assert build_decision_context(r.engine.snapshot()) == ctx_of(sc)


# ---------------------------------------------------------------------------- contents

def test_healthy_context_exposes_all_layers():
    ctx = ctx_of(traded_script())
    q = ctx.quality
    assert q.market_data_ok and q.book_state == "valid" and q.connection == "connected" and q.alerts == ()
    p = ctx.price
    assert p.available and p.mid_x2 == 2 * 84000 + 1 and p.spread_units == 1
    assert p.micro_num is not None and p.microprice_units == pytest.approx(84000.5)
    assert ctx.flow.available and ctx.metrics is not None
    assert ctx.structure_section.available and ctx.patterns_section.available and ctx.absorption_section.available
    assert ctx.session.available and ctx.session.in_rth and ctx.session.session.session.volume > 0
    assert ctx.notes == EPISTEMIC_NOTES and any("MBP" in n for n in ctx.notes)


def test_bar_context_summarises_completed_bars_exactly():
    ctx = ctx_of(traded_script())
    b1 = ctx.bars_1m
    assert b1.available and b1.timeframe_s == 60 and b1.bars_used >= 5
    assert b1.reason.startswith("partial_lookback") and b1.lookback == 10
    assert b1.latest is not None and b1.latest.end_s <= ctx.wall_ns // S
    assert b1.volume == b1.buy_volume + b1.sell_volume + b1.unknown_volume
    assert b1.known_delta == b1.buy_volume - b1.sell_volume
    assert b1.high >= b1.close >= b1.low and b1.up_bars + b1.down_bars + b1.flat_bars == b1.traded_bars
    assert b1.quality_ok and b1.vwap_units == pytest.approx(b1.vwap_num / b1.volume)
    b30 = ctx.bars_30s
    assert b30.bars_used == 10 and b30.reason == "ok"            # full lookback available


def test_session_vwap_relation_is_exact_integer_arithmetic():
    ctx = ctx_of(traded_script())
    st = ctx.session.session.session
    exp = (ctx.price.mid_x2 * st.volume - 2 * st.vwap_num) / (2 * st.volume)
    assert ctx.session.vs_vwap_units == pytest.approx(exp)
    assert ctx.session.vs_vwap_num == ctx.price.mid_x2 * st.volume - 2 * st.vwap_num


# ---------------------------------------------------------------------------- unavailable evidence

def test_before_book_valid_nothing_is_fabricated():
    sc = RawScript(wall0=T0 * S)
    sc.bootstrap(**WEEK)
    ctx = ctx_of(sc)
    assert not ctx.quality.market_data_ok and not ctx.price.available
    assert ctx.price.mid_x2 is None and ctx.price.micro_num is None and ctx.price.spread_units is None
    for b in (ctx.bars_5m, ctx.bars_1m, ctx.bars_30s):
        assert not b.available and b.reason == "no_completed_bars" and b.high is None and b.net_change_units is None
    assert not ctx.structure_section.available and not ctx.absorption_section.available


def test_stale_and_suspect_book_make_price_and_c8_unavailable():
    sc = traded_script(12)
    sc.depth(DEPTH, 9, 1, 1, BASE, 1)                       # structural violation -> STALE
    ctx = ctx_of(sc)
    assert ctx.quality.book_state == "stale" and not ctx.quality.market_data_ok
    assert not ctx.price.available and ctx.price.reason == "book_stale" and ctx.price.mid_x2 is None
    assert ctx.price.bbo_bid is not None                    # the BBO is still reported, as BBO only
    assert not ctx.structure_section.available and not ctx.absorption_section.available
    assert ctx.session.vs_vwap_num is None                  # no valid mid -> no VWAP relation


def test_continuity_break_is_visible_in_epochs_and_sections():
    sc = traded_script(12)
    before = ctx_of(sc)
    sc.error(-1, 1100)
    after = ctx_of(sc)
    assert after.quality.connection == "lost" and not after.quality.market_data_ok
    assert after.quality.structure_epoch > before.quality.structure_epoch
    assert after.quality.tape_epoch > before.quality.tape_epoch
    assert not after.structure_section.available and not after.patterns_section.available
    assert after.quality.bar_active_flags & int(BarFlag.CONNECTION_INTERRUPTION)


def test_bars_disabled_and_unknown_calendar():
    ctx = ctx_of(traded_script(6), bars=BarsConfig(enabled=False))
    assert ctx.bars_1m.reason == "bars_disabled" and not ctx.bars_1m.available
    sc = RawScript(wall0=T0 * S)
    sc.bootstrap(trading_hours="", liquid_hours="", time_zone_id="US/Central")
    sc.seed_book()
    ticks(sc, 600)
    c2 = ctx_of(sc)
    assert not c2.session.available and c2.session.reason.startswith("calendar_unknown")
    assert not c2.quality.session_calendar_ok


def test_gap_in_lookback_marks_bar_quality():
    sc = traded_script(20)
    sc.error(-1, 1100)
    sc.at(T0 + 250)
    sc.error(-1, 1102)
    for i in range(10):
        sc.at(T0 + 260 + i * 9)
        sc.trade(TRADES, BASE, 1)
        sc.tick()
    b = ctx_of(sc).bars_1m
    assert not b.quality_ok and b.flags & int(BarFlag.DATA_GAP)


# ---------------------------------------------------------------------------- UNKNOWN stays UNKNOWN

def test_unknown_volume_is_never_redistributed():
    # special condition "Z": classifier-ineligible (UNKNOWN) but allowed into bars by config
    sc = traded_script(30, special="Z")
    ctx = ctx_of(sc, bars=BarsConfig(allowed_special_conditions="Z"))
    for b in (ctx.bars_1m, ctx.bars_30s):
        assert b.volume > 0 and b.unknown_volume == b.volume
        assert b.buy_volume == 0 and b.sell_volume == 0 and b.known_delta == 0
    tf = ctx.metrics.trade_flow[-1]
    assert tf.unknown_volume > 0 and tf.buy_volume == 0 and tf.sell_volume == 0


# ---------------------------------------------------------------------------- config

def test_lookbacks_are_configurable_and_bounded():
    ctx = ctx_of(traded_script(), cfg=DecisionConfig(lookback_30s=3, lookback_1m=2, lookback_5m=1))
    assert ctx.bars_30s.bars_used == 3 and ctx.bars_1m.bars_used == 2 and ctx.bars_5m.lookback == 1
    with pytest.raises(ConfigError):
        config_from_mapping({"decision": {"lookback_1m": 11}})          # > [bars].snapshot_bars
    with pytest.raises(ConfigError):
        config_from_mapping({"decision": {"lookback_5m": 0}})


@pytest.mark.parametrize("mode", ["AUTONOMOUS", "autonomous", "LIVE", ""])
def test_only_human_approval_mode_exists(mode):
    with pytest.raises(ConfigError, match="no autonomous execution"):
        config_from_mapping({"decision": {"mode": mode}})
    assert config_from_mapping({}).decision.mode == "HUMAN_APPROVAL"


def test_bar_context_helper_without_bars():
    c = bar_context(None, 60, 5)
    assert not c.available and c.reason == "bars_disabled" and c.volume == 0 and c.latest is None
    assert dataclasses.is_dataclass(c)
