"""C5 bars: canonical 30 s bars, 1 m / 5 m aggregation, closing, late prints, empty bars,
quality flags, eligibility, determinism and snapshots. Event time only; no sleeps."""

from __future__ import annotations

import dataclasses
import random
from datetime import datetime, timezone

import pytest

from hermes.config import BarsConfig, ConfigError, config_from_mapping
from hermes.market.bars import (
    Bar,
    BarEligibilityPolicy,
    BarEngine,
    BarExclusion,
    BarFlag,
    TradeDisposition,
)
from hermes.market.classify import Aggressor
from hermes.market.sessions import SessionCalendar
from tests.support import TRADES, Harness, RawScript

S = 1_000_000_000
MS = 1_000_000
BUY, SELL, UNK = Aggressor.BUY, Aggressor.SELL, Aggressor.UNKNOWN
F = BarFlag


def utc(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


T0 = utc("2026-09-22T14:00:00")          # Tuesday 09:00 CDT: inside the trading session and RTH
WEEK = ("20260920:1700-20260921:1600;20260921:1700-20260922:1600;20260922:1700-20260923:1600;"
        "20260923:1700-20260924:1600;20260924:1700-20260925:1600;20260926:CLOSED;20260927:1700-20260928:1600")
WEEK_RTH = "20260921:0830-20260921:1500;20260922:0830-20260922:1500;20260923:0830-20260923:1500"
CAL = SessionCalendar("US/Central", WEEK, WEEK_RTH)


def engine(cal=CAL, **kw) -> BarEngine:
    b = BarEngine(dataclasses.replace(BarsConfig(), **kw), 1)
    b.set_calendar(cal)
    b.armed = True
    b.advance(T0 * S)                     # observation starts at T0
    return b


def trade(b: BarEngine, ts, price, size=1, aggr=BUY, seq=None, excl=None):
    seq = seq if seq is not None else trade.seq
    trade.seq = seq + 1
    return b.on_trade(int(ts), price, size, seq, aggr, excl)


trade.seq = 1


def direct(trades, start, end):
    """Reference bar computed straight from the prints (no aggregation)."""
    ins = [t for t in trades if start <= t[0] < end]
    if not ins:
        return None
    buy = sum(t[2] for t in ins if t[3] is BUY)
    sell = sum(t[2] for t in ins if t[3] is SELL)
    return dict(open=ins[0][1], high=max(t[1] for t in ins), low=min(t[1] for t in ins), close=ins[-1][1],
                volume=sum(t[2] for t in ins), trades=len(ins), buy_volume=buy, sell_volume=sell,
                unknown_volume=sum(t[2] for t in ins if t[3] is UNK), known_delta=buy - sell,
                vwap_num=sum(t[1] * t[2] for t in ins), first_seq=ins[0][4], last_seq=ins[-1][4])


def fields(bar: Bar) -> dict:
    return {k: getattr(bar, k) for k in ("open", "high", "low", "close", "volume", "trades", "buy_volume",
                                         "sell_volume", "unknown_volume", "known_delta", "vwap_num",
                                         "first_seq", "last_seq")}


# ---------------------------------------------------------------------------- 30 s bars

def test_membership_by_exchange_time_boundary_goes_to_next_bar():
    b = engine()
    trade(b, T0 + 29, 100, seq=1)
    trade(b, T0 + 30, 101, seq=2)          # exactly :30 -> next bar
    b.advance((T0 + 60) * S + 500 * MS)
    b0, b1 = b.completed_bars(30)
    assert (b0.start_s, b0.end_s, b0.close, b0.trades) == (T0, T0 + 30, 100, 1)
    assert (b1.start_s, b1.open, b1.trades) == (T0 + 30, 101, 1)


def test_ohlc_volume_flow_vwap_and_seq_range():
    b = engine()
    trade(b, T0 + 1, 100, 2, BUY, seq=10)
    trade(b, T0 + 5, 104, 1, SELL, seq=11)
    trade(b, T0 + 9, 98, 3, UNK, seq=12)
    trade(b, T0 + 20, 101, 4, BUY, seq=13)
    b.advance((T0 + 30) * S + 500 * MS)
    (bar,) = b.completed_bars(30)
    assert (bar.open, bar.high, bar.low, bar.close) == (100, 104, 98, 101)
    assert (bar.volume, bar.trades, bar.buy_volume, bar.sell_volume, bar.unknown_volume) == (10, 4, 6, 1, 3)
    assert bar.known_delta == 5                                  # UNKNOWN never folded in
    assert bar.vwap_num == 200 + 104 + 294 + 404 and bar.vwap == pytest.approx(1002 / 10)
    assert (bar.first_seq, bar.last_seq, bar.flags, bar.trading_date) == (10, 13, F.NONE, "20260922")


def test_bar_is_final_only_after_end_plus_grace():
    b = engine()
    trade(b, T0 + 3, 100)
    b.advance((T0 + 30) * S + 499 * MS)
    assert b.completed[30] == 0 and b.forming()[0].flags & F.FORMING
    b.advance((T0 + 30) * S + 500 * MS)
    assert b.completed[30] == 1 and b.forming()[0] is None


def test_prints_during_grace_still_count_and_late_prints_never_rewrite():
    b = engine()
    trade(b, T0 + 10, 100, 1)
    b.advance((T0 + 30) * S + 200 * MS)
    assert trade(b, T0 + 29, 102, 2) is TradeDisposition.INCLUDED     # delayed but inside the grace window
    trade(b, T0 + 31, 103, 1)                                          # next bar forming
    b.advance((T0 + 30) * S + 600 * MS)                                # bar 0 now final
    before = b.completed_bars(30)[0]
    assert (before.volume, before.close) == (3, 102)
    assert trade(b, T0 + 29, 999, 7) is TradeDisposition.LATE
    assert b.completed_bars(30)[0] is before                           # never rewritten
    assert (b.late_trades, b.late_volume) == (1, 7)
    b.advance((T0 + 60) * S + 500 * MS)
    nxt = b.completed_bars(30)[1]
    assert nxt.volume == 1 and nxt.high == 103                         # never moved into a later bar
    assert nxt.flags & F.LATE_DATA_OBSERVED


def test_empty_bars_inside_session_after_first_price_only():
    b = engine()
    b.advance((T0 + 90) * S + 500 * MS)                                # no price yet: no bars at all
    assert b.completed[30] == 0
    trade(b, T0 + 95, 100)
    b.advance((T0 + 210) * S + 500 * MS)
    bars = b.completed_bars(30)
    assert [x.start_s for x in bars] == [T0 + 90, T0 + 120, T0 + 150, T0 + 180]
    assert bars[0].volume == 1 and not bars[0].flags & F.EMPTY
    for e in bars[1:]:
        assert e.flags & F.EMPTY and e.volume == 0 and (e.open, e.high, e.low, e.close) == (100,) * 4
        assert e.first_seq is None and e.trades == 0
    assert b.empty_bars == 3


def test_no_bars_through_maintenance_break_or_weekend():
    b = engine()
    close = utc("2026-09-22T21:00:00")                                 # 16:00 CDT daily close
    trade(b, close - 5, 100)
    b.advance((close + 3 * 3600) * S)                                  # through the break and into the next session
    bars = b.completed_bars(30)
    assert bars[-1].start_s > close + 3600 - 1                         # resumed at the 17:00 open
    assert not [x for x in bars if close <= x.start_s < close + 3600]  # nothing inside the break
    assert bars[0].end_s == close and bars[0].flags & F.SESSION_BOUNDARY
    first_open = next(x for x in bars if x.start_s >= close)
    assert first_open.start_s == close + 3600 and first_open.flags & F.SESSION_BOUNDARY and first_open.flags & F.EMPTY
    assert first_open.trading_date == "20260923"
    assert not [x for x in bars if x.flags & F.DATA_GAP]               # a closure is not a gap
    # weekend: Friday close -> Sunday open, zero bars in between
    fri = utc("2026-09-25T21:00:00")
    b.advance((fri + 2 * 86400) * S)
    assert not [x for x in b.completed_bars(30) if fri <= x.start_s < utc("2026-09-27T22:00:00")]


def test_unknown_calendar_never_fabricates_empty_bars():
    b = engine(cal=SessionCalendar("US/Central", "", ""))
    trade(b, T0 + 1, 100)
    b.advance((T0 + 600) * S)
    assert b.completed[30] == 1 and b.completed_bars(30)[0].trading_date == ""


def test_history_is_bounded_fifo():
    b = engine(history_30s=3, history_1m=2, history_5m=1)
    for i in range(20):
        trade(b, T0 + 30 * i, 100 + i)
    b.advance((T0 + 600) * S + 500 * MS)
    assert [x.open for x in b.completed_bars(30)] == [117, 118, 119]
    assert len(b.completed_bars(60)) == 2 and len(b.completed_bars(300)) == 1
    assert b.completed[30] >= 20


# ---------------------------------------------------------------------------- aggregation

def random_trades(n_min=12, seed=7):
    rng = random.Random(seed)
    out, seq, p = [], 1, 84000
    for sec in range(n_min * 60):
        if rng.random() < 0.35:                           # quiet seconds -> some EMPTY 30 s bars
            continue
        for _ in range(rng.randint(1, 3)):
            p += rng.choice((-2, -1, 0, 1, 2))
            out.append((T0 + sec, p, rng.randint(1, 9), rng.choice((BUY, SELL, UNK)), seq))
            seq += 1
    return out


def test_1m_and_5m_aggregates_equal_direct_computation():
    trades = random_trades()
    b = engine()
    for ts, p, sz, a, seq in trades:
        b.on_trade(ts, p, sz, seq, a, None)
        b.advance(ts * S)                                  # watermark follows the prints
    b.advance((T0 + 12 * 60) * S + 500 * MS)
    for tf in (30, 60, 300):
        bars = b.completed_bars(tf)
        assert bars and all(x.end_s - x.start_s == tf and x.start_s % tf == 0 for x in bars)
        for x in bars:
            ref = direct(trades, x.start_s, x.end_s)
            if ref is None:
                assert x.flags & F.EMPTY and x.volume == 0
            else:
                assert fields(x) == ref, (tf, x.start_s)
    assert len(b.completed_bars(60)) == 12 and len(b.completed_bars(300)) == 2
    # totals are identical at every level
    for key in ("volume", "buy_volume", "sell_volume", "unknown_volume", "known_delta", "vwap_num", "trades"):
        tot = {tf: sum(getattr(x, key) for x in b.completed_bars(tf) if x.start_s < T0 + 600) for tf in (30, 60, 300)}
        assert tot[30] == tot[60] == tot[300], key


def test_aggregate_flags_are_ored_and_partial_on_missing_children():
    b = engine()
    b.advance((T0 + 45) * S)                                # observation really starts mid-minute
    b.obs_start_ns = (T0 + 45) * S
    trade(b, T0 + 50, 100)
    b.mark((T0 + 70) * S, F.DATA_GAP)                       # gap inside the second minute
    trade(b, T0 + 75, 101)
    b.advance((T0 + 300) * S + 500 * MS)
    m0, m1 = b.completed_bars(60)[:2]
    assert m0.flags & F.PARTIAL                             # 30 s child [30,60) partial -> inherited
    assert m1.flags & F.DATA_GAP and not m1.flags & F.PARTIAL
    (five,) = b.completed_bars(300)
    assert five.flags & F.DATA_GAP and five.flags & F.PARTIAL and not five.flags & F.EMPTY


def test_aggregate_all_empty_children_is_empty_flat():
    b = engine()
    trade(b, T0 + 1, 100)
    b.advance((T0 + 300) * S + 500 * MS)
    ones = b.completed_bars(60)
    assert not ones[0].flags & F.EMPTY
    assert all(x.flags & F.EMPTY and x.close == 100 for x in ones[1:])


def test_forming_views():
    b = engine()
    trade(b, T0 + 1, 100, 2)
    trade(b, T0 + 31, 105, 1)
    b.advance((T0 + 30) * S + 600 * MS)                     # first 30 s bar final
    trade(b, T0 + 40, 95, 3)
    f30, f60, f300 = b.forming()
    assert all(x.flags & F.FORMING for x in (f30, f60, f300))
    assert (f30.start_s, f30.volume) == (T0 + 30, 4)
    assert (f60.open, f60.high, f60.low, f60.close, f60.volume) == (100, 105, 95, 95, 6)
    assert f300.volume == 6 and f300.start_s == T0


# ---------------------------------------------------------------------------- eligibility

def test_bar_eligibility_policy_conservative_defaults_and_config():
    p = BarEligibilityPolicy(BarsConfig())
    assert p.evaluate(1, False, False, "") is None
    assert p.evaluate(0, False, False, "") is BarExclusion.NON_POSITIVE_SIZE
    assert p.evaluate(1, True, False, "") is BarExclusion.PAST_LIMIT
    assert p.evaluate(1, False, True, "") is BarExclusion.UNREPORTED
    assert p.evaluate(1, False, False, "X") is BarExclusion.SPECIAL_CONDITION
    q = BarEligibilityPolicy(BarsConfig(include_past_limit=True, include_unreported=True,
                                        allowed_special_conditions="X"))
    assert q.evaluate(1, True, True, "X") is None


def test_excluded_prints_are_counted_but_never_touch_ohlc_or_volume():
    b = engine()
    trade(b, T0 + 1, 100, 1)
    assert trade(b, T0 + 2, 500, 9, excl=BarExclusion.PAST_LIMIT) is TradeDisposition.EXCLUDED
    b.advance((T0 + 30) * S + 500 * MS)
    (bar,) = b.completed_bars(30)
    assert (bar.high, bar.volume, bar.excluded_trades, bar.excluded_volume) == (100, 1, 1, 9)
    assert b.excluded_by_reason == {"past_limit": 1}


# ---------------------------------------------------------------------------- engine integration

WEEK_CD = dict(trading_hours=WEEK, liquid_hours=WEEK_RTH, time_zone_id="US/Central")


def live_script(t0=T0) -> RawScript:
    sc = RawScript(wall0=t0 * S)
    sc.bootstrap(**WEEK_CD)
    sc.seed_book()
    return sc


def bars_of(h: Harness, tf=30):
    return h.engine.instruments[1].bars.completed_bars(tf)


def test_engine_builds_bars_from_classified_trades_and_closes_on_ticks():
    sc = live_script()
    sc.at(T0 + 1)
    sc.trade(TRADES, 21000.25, 2)            # at the ask -> BUY
    sc.at(T0 + 2)
    sc.trade(TRADES, 21000.0, 1)             # at the bid -> SELL
    sc.at(T0 + 3)
    sc.trade(TRADES, 21000.0, 4, special="Z")   # classifier-ineligible AND bar-excluded (default)
    sc.at(T0 + 30.4)
    sc.tick()
    h = Harness().run(sc)
    assert bars_of(h) == ()
    sc.at(T0 + 30.5)
    sc.tick()
    h = Harness().run(sc)
    (bar,) = bars_of(h)
    assert (bar.buy_volume, bar.sell_volume, bar.unknown_volume, bar.volume, bar.excluded_trades) == (2, 1, 0, 3, 1)
    assert (bar.open, bar.close, bar.trading_date) == (84001, 84000, "20260922")
    snap = h.engine.snapshot().instruments[0]
    assert snap.bars.latest_30s == (bar,) and snap.session.in_rth and snap.session.session.volume == 3


def test_classifier_ineligible_print_can_still_be_bar_eligible_as_unknown():
    from hermes.config import TapeConfig
    sc = live_script()
    sc.at(T0 + 1)
    sc.trade(TRADES, 21000.25, 5, special="Q")
    sc.at(T0 + 31)
    sc.tick()
    h = Harness(tape=TapeConfig(), bars=BarsConfig(allowed_special_conditions="Q")).run(sc)
    (bar,) = bars_of(h)
    assert h.engine.instruments[1].tape.last().eligible is False
    assert (bar.volume, bar.unknown_volume, bar.buy_volume) == (5, 5, 0)


def test_disconnect_flags_are_sticky_and_empty_outage_bars_are_gaps():
    sc = live_script()
    sc.at(T0 + 1)
    sc.trade(TRADES, 21000.25, 1)
    sc.at(T0 + 40)
    sc.error(-1, 1100, "connectivity lost")          # outage from :40
    sc.at(T0 + 100)
    sc.error(-1, 1102, "restored, data maintained")  # back at 1:40
    sc.at(T0 + 101)
    sc.trade(TRADES, 21000.25, 1)
    sc.at(T0 + 200)
    sc.tick()
    h = Harness().run(sc)
    bars = {x.start_s - T0: x for x in bars_of(h)}
    assert not bars[0].flags & F.DATA_GAP
    for k in (30, 60, 90):                           # every bar overlapping the outage
        assert bars[k].flags & (F.DATA_GAP | F.CONNECTION_INTERRUPTION | F.MARKET_DATA_INVALID) == \
            F.DATA_GAP | F.CONNECTION_INTERRUPTION | F.MARKET_DATA_INVALID, k
    assert bars[30].flags & F.EMPTY and bars[60].flags & F.EMPTY  # empty but explicitly a gap, not "no trading"
    assert not bars[120].flags & F.DATA_GAP          # after recovery: clean again
    assert bars[90].volume == 1                      # the recovered print; its bar keeps the flag
    ones = bars_of(h, 60)
    assert ones[0].flags & F.DATA_GAP and ones[1].flags & F.CONNECTION_INTERRUPTION
    snap = h.engine.snapshot().instruments[0]
    assert snap.bars.active_flags == F.NONE and snap.bars.gap_bars == 3 and snap.session.gap_observed


def test_10197_and_trades_resubscription_flag_bars():
    sc = live_script()
    sc.at(T0 + 1)
    sc.trade(TRADES, 21000.25, 1)
    sc.at(T0 + 35)
    sc.error(-1, 10197, "competing session")
    sc.at(T0 + 70)
    sc.request("reqTickByTickData", TRADES + 100, tick_type="AllLast")   # new trades generation
    sc.at(T0 + 100)
    sc.tick()
    h = Harness().run(sc)
    bars = {x.start_s - T0: x for x in bars_of(h)}
    assert bars[30].flags & F.MARKET_DATA_INVALID and bars[30].flags & F.DATA_GAP
    assert not bars[30].flags & F.CONNECTION_INTERRUPTION
    assert bars[60].flags & F.DATA_GAP
    assert h.engine.snapshot().instruments[0].bars.active_flags & F.MARKET_DATA_INVALID   # conflict still active


def test_startup_first_subscription_is_not_a_gap():
    sc = live_script()
    sc.at(T0 + 5)
    sc.trade(TRADES, 21000.25, 1)
    sc.at(T0 + 31)
    sc.tick()
    (bar,) = bars_of(Harness().run(sc))
    assert bar.flags == F.NONE


def test_session_volume_equals_sum_of_bars_and_state_token_changes_on_close():
    sc = live_script()
    prices = [21000.25, 21000.0, 21000.25, 21000.5, 21000.0]
    for i, p in enumerate(prices * 6):
        sc.at(T0 + 1 + i * 7)
        sc.trade(TRADES, p, 1 + i % 3)
        sc.tick()
    sc.at(T0 + 240)
    sc.tick()
    h = Harness().run(sc)
    snap = h.engine.snapshot().instruments[0]
    assert snap.session.session.volume == sum(x.volume for x in bars_of(h))
    assert snap.session.session.vwap_num == sum(x.vwap_num for x in bars_of(h))
    tok = h.engine.state_token()
    sc.at(T0 + 270.5)
    sc.tick()
    h2 = Harness().run(sc)
    assert h2.engine.state_token() != tok


def test_replay_is_deterministic():
    sc = live_script()
    rng = random.Random(3)
    for i in range(300):
        sc.at(T0 + i * 1.2)
        if rng.random() < 0.6:
            sc.trade(TRADES, 21000 + 0.25 * rng.randint(-4, 4), rng.randint(1, 5))
        if i % 3 == 0:
            sc.tick()
        if i == 150:
            sc.error(-1, 1100)
        if i == 160:
            sc.error(-1, 1102)
    a, b = Harness().run(sc), Harness().run(sc)
    for tf in (30, 60, 300):
        assert bars_of(a, tf) == bars_of(b, tf) and bars_of(a, tf)
    assert a.engine.snapshot() == b.engine.snapshot()


def test_bars_can_be_disabled():
    sc = live_script()
    sc.at(T0 + 1)
    sc.trade(TRADES, 21000.25, 1)
    h = Harness(bars=BarsConfig(enabled=False)).run(sc)
    assert h.engine.instruments[1].bars is None and h.engine.snapshot().instruments[0].bars is None


def test_bars_config_validation():
    with pytest.raises(ConfigError):
        config_from_mapping({"bars": {"close_grace_ms": -1}})
    with pytest.raises(ConfigError):
        config_from_mapping({"bars": {"history_30s": 0}})
    with pytest.raises(ConfigError):
        config_from_mapping({"bars": {"typo": 1}})
    assert config_from_mapping({}).bars.close_grace_ms == 500



def test_dst_fall_2026_bars_resume_at_new_utc_open_without_fake_bars():
    from tests.unit.test_sessions import FALL_LIQUID, FALL_TRADING
    cal = SessionCalendar("US/Central", FALL_TRADING, FALL_LIQUID)
    fri_close = utc("2026-10-30T21:00:00")                    # 16:00 CDT
    b = BarEngine(BarsConfig(), 1)
    b.set_calendar(cal)
    b.armed = True
    b.advance((fri_close - 60) * S)
    b.on_trade(fri_close - 10, 100, 1, 1, BUY, None)
    b.advance(utc("2026-11-01T23:02:00") * S)
    bars = b.completed_bars(30)
    after = [x for x in bars if x.start_s >= fri_close]
    assert after[0].start_s == utc("2026-11-01T23:00:00")    # 17:00 CST (was 22:00 UTC under CDT)
    assert not [x for x in after if x.start_s < utc("2026-11-01T23:00:00")]
    assert after[0].trading_date == "20261102" and after[0].flags & F.SESSION_BOUNDARY
    assert bars[0].trading_date == "20261030"


def test_snapshot_latest_bars_newest_first_and_bounded():
    sc = live_script()
    for i in range(8):
        sc.at(T0 + 1 + 30 * i)
        sc.trade(TRADES, 21000.25, 1)
    sc.at(T0 + 241)
    sc.tick()
    snap = Harness(bars=BarsConfig(snapshot_bars=3)).run(sc).engine.snapshot().instruments[0].bars
    assert [x.start_s - T0 for x in snap.latest_30s] == [210, 180, 150]
    assert snap.completed_30s == 8 and snap.forming_30s is None and snap.latest_1m[0].start_s == T0 + 180


def test_bar_report_tool_runs_deterministically(capsys):
    from tools import bar_report
    assert bar_report.main(["--synthetic", "400", "--no-perf", "--tf", "30", "--last", "3"]) == 0
    first = capsys.readouterr().out
    assert bar_report.main(["--synthetic", "400", "--no-perf", "--tf", "30", "--last", "3"]) == 0
    assert capsys.readouterr().out == first and "bars 30s/1m/5m" in first and "late prints" in first
