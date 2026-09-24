"""Tape bounds/totals and C4 engine integration (generations, health, reordering, snapshots)."""

from __future__ import annotations

import dataclasses
import random

from hermes.config import TapeConfig
from hermes.market.classify import Aggressor, ClassMethod, UnknownReason
from hermes.market.tape import ClassifiedTrade, Tape
from tests.support import BASE, BBO, TICK, TRADES, Harness, RawScript

MS = 1_000_000
BUY, SELL, UNK = Aggressor.BUY, Aggressor.SELL, Aggressor.UNKNOWN


def trade(seq, t_ms, size=1, side=BUY):
    return ClassifiedTrade(1, seq, 1, 0, 0, t_ms * MS, t_ms * MS, 84000, size, "CME", "", False, False, True, side,
                           ClassMethod.DIRECT_QUOTE if side is not UNK else ClassMethod.NONE,
                           1.0 if side is not UNK else 0.0, None if side is not UNK else UnknownReason.NO_QUOTE,
                           84000, 84001, 1, 1, True)


def check_totals(tape: Tape):
    ts = tape.trades()
    r = tape.retained_window
    assert r.buy_volume == sum(t.size for t in ts if t.aggressor is BUY)
    assert r.sell_volume == sum(t.size for t in ts if t.aggressor is SELL)
    assert r.unknown_volume == sum(t.size for t in ts if t.aggressor is UNK)
    assert r.buy_trades + r.sell_trades + r.unknown_trades == len(ts)


# ---------------------------------------------------------------------------
# Tape bounds and totals
# ---------------------------------------------------------------------------

def test_eviction_by_count_is_deterministic():
    tape = Tape(dataclasses.replace(TapeConfig(), max_trades=3))
    for i in range(5):
        tape.append(trade(i, i, size=i + 1))
    assert [t.seq for t in tape.trades()] == [2, 3, 4] and tape.evicted_by_count == 2
    check_totals(tape)
    assert tape.session_cumulative.buy_volume == 15 and tape.retained_window.buy_volume == 12


def test_eviction_by_age_on_append_and_on_tick():
    tape = Tape(dataclasses.replace(TapeConfig(), max_age_s=1.0))
    tape.append(trade(1, 0))
    tape.append(trade(2, 500))
    tape.append(trade(3, 1000))                  # 1000 - 1000 = 0: trade at 0 is exactly at the horizon, kept
    assert len(tape) == 3
    tape.append(trade(4, 1001))                  # now trade 1 (t=0) is older than 1 s
    assert [t.seq for t in tape.trades()] == [2, 3, 4] and tape.evicted_by_age == 1
    tape.evict_by_age(2600 * MS)                 # clock tick with no trades
    assert tape.trades() == () and tape.evicted_by_age == 4
    check_totals(tape)
    assert tape.retained_window.frozen().buy_volume == 0 and tape.session_cumulative.buy_volume == 4


def test_session_epoch_and_window_totals_are_distinct():
    tape = Tape(dataclasses.replace(TapeConfig(), max_trades=2))
    tape.append(trade(1, 0, size=5, side=BUY))
    tape.append(trade(2, 1, size=3, side=SELL))
    tape.new_epoch()                                 # continuity break: epoch totals restart
    tape.append(trade(3, 2, size=7, side=UNK))
    tape.append(trade(4, 3, size=2, side=BUY))
    s, e, w = tape.session_cumulative.frozen(), tape.epoch_cumulative.frozen(), tape.retained_window.frozen()
    assert (s.buy_volume, s.sell_volume, s.unknown_volume, s.known_delta) == (7, 3, 7, 4)
    assert (e.buy_volume, e.sell_volume, e.unknown_volume, e.known_delta) == (2, 0, 7, 2)
    assert (w.buy_volume, w.sell_volume, w.unknown_volume, w.known_delta) == (2, 0, 7, 2)   # last 2 trades
    for tot in (s, e, w):
        assert tot.known_delta == tot.buy_volume - tot.sell_volume          # UNKNOWN never included


def test_unknown_volume_is_never_folded_into_delta():
    tape = Tape(TapeConfig())
    tape.append(trade(1, 0, size=5, side=BUY))
    tape.append(trade(2, 1, size=3, side=SELL))
    tape.append(trade(3, 2, size=7, side=UNK))
    f = tape.retained_window.frozen()
    assert (f.buy_volume, f.sell_volume, f.unknown_volume, f.known_delta) == (5, 3, 7, 2)
    assert dict(f.by_method) == {"direct_quote": 2, "none": 1}


def test_latest_is_bounded_and_newest_first():
    tape = Tape(TapeConfig())
    for i in range(50):
        tape.append(trade(i, i))
    assert [t.seq for t in tape.latest(3)] == [49, 48, 47]


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------

def ready():
    s = RawScript().bootstrap().seed_book()
    s.advance(600)
    s.tick()
    h = Harness().run(s)
    assert h.engine.snapshot().instruments[0].market_data_ok
    return h, s


def step(h, s, fn, *a, **kw):
    n = len(s.events)
    fn(*a, **kw)
    h.run(s, n)


def tape(h):
    return h.engine.snapshot().instruments[0].tape


def last(h):
    return h.engine.instruments[1].tape.last()


def test_live_classification_through_engine():
    h, s = ready()                                    # BBO 21000.00 / 21000.25
    step(h, s, s.trade, TRADES, BASE + TICK, 3)
    t = last(h)
    assert (t.aggressor, t.method, t.size, t.generation, t.quote_bid_units, t.quote_ask_units) == (
        BUY, ClassMethod.DIRECT_QUOTE, 3, TRADES, 84000, 84001)
    assert t.book_valid and t.eligible and t.tape_epoch == tape(h).epoch
    step(h, s, s.trade, TRADES, BASE, 2)
    assert last(h).aggressor is SELL
    step(h, s, s.trade, TRADES, BASE, 4, unreported=True)
    assert (last(h).aggressor, last(h).unknown_reason) == (UNK, UnknownReason.INELIGIBLE)
    ts = tape(h)
    assert ts.session_cumulative == ts.epoch_cumulative == ts.retained_window
    assert (ts.retained_window.buy_volume, ts.retained_window.sell_volume, ts.retained_window.unknown_volume) == (3, 2, 4)
    assert ts.last_aggressor is UNK and ts.size == 3 and ts.context_ok and ts.has_quote


def test_old_generation_trade_and_quote_are_ignored():
    h, s = ready()
    step(h, s, s.request, "cancelTickByTickData", TRADES)
    step(h, s, s.request, "reqTickByTickData", 20_003, tick_type="AllLast")
    step(h, s, s.trade, TRADES, BASE + TICK, 9)       # late print from the OLD trades generation
    assert tape(h).size == 0
    step(h, s, s.request, "cancelTickByTickData", BBO)
    step(h, s, s.request, "reqTickByTickData", 20_002, tick_type="BidAsk")
    step(h, s, s.bbo, BBO, BASE - 5, BASE + 5)        # late quote from the OLD BBO generation
    assert not tape(h).has_quote
    step(h, s, s.trade, 20_003, BASE + TICK, 1)
    assert (last(h).aggressor, last(h).unknown_reason) == (UNK, UnknownReason.INVALID_CONTEXT)
    step(h, s, s.bbo, 20_002, BASE, BASE + TICK)
    step(h, s, s.trade, 20_003, BASE + TICK, 1)
    assert (last(h).aggressor, last(h).generation) == (BUY, 20_003)


def test_invalid_health_context_forces_unknown():
    h, s = ready()
    step(h, s, s.error, -1, 2103, "Market data farm connection is broken")
    step(h, s, s.trade, TRADES, BASE + TICK, 1)
    assert (last(h).aggressor, last(h).unknown_reason) == (UNK, UnknownReason.INVALID_CONTEXT)
    assert tape(h).context_reason == "farm:broken" and not tape(h).context_ok
    h, s = ready()
    step(h, s, s.error, -1, 10197, "No market data during competing live session")
    step(h, s, s.trade, TRADES, BASE + TICK, 1)
    assert last(h).unknown_reason is UnknownReason.INVALID_CONTEXT


def test_bbo_rejection_degrades_classification():
    h, s = ready()
    step(h, s, s.error, BBO, 10190, "Max number of tick-by-tick requests")
    step(h, s, s.trade, TRADES, BASE + TICK, 1)
    assert last(h).unknown_reason is UnknownReason.INVALID_CONTEXT


def test_snapshot_never_exposes_pre_reset_classifier_state():
    h, s = ready()
    step(h, s, s.trade, TRADES, BASE + TICK, 1)
    before = tape(h)
    assert before.has_quote and before.tick_direction is None
    step(h, s, s.closed)                              # disconnect: continuity broken
    after = tape(h)
    assert not after.has_quote and after.quote_bid_units is None and after.tick_direction is None
    assert after.epoch == before.epoch + 1 and after.classifier_epoch > before.classifier_epoch
    assert not after.context_ok and after.size == 1   # real prints are kept, flagged by epoch
    assert after.epoch_cumulative.buy_volume == 0 and after.session_cumulative.buy_volume == 1


def test_reset_publishes_immediately_through_pipeline():
    """state_token covers the tape/classifier: the published snapshot changes in the same callback."""
    from hermes.config import BookConfig, SessionConfig, SubscriptionsConfig
    from hermes.core.telemetry import Telemetry
    from hermes.ibkr.adapter import RawPipeline
    from hermes.ibkr.normalizer import Normalizer
    from hermes.market.engine import MarketEngine
    from hermes.market.snapshot import SnapshotPublisher

    eng = MarketEngine(BookConfig(), SessionConfig(), SubscriptionsConfig())
    p = RawPipeline(Normalizer(), eng, Telemetry(), SnapshotPublisher(), None, snapshot_interval_ns=10**12)
    s = RawScript().bootstrap().seed_book()
    s.advance(600)
    s.tick()

    def feed(evs):
        for ev in evs:
            fields = {f: getattr(ev, f) for f in ev.__dataclass_fields__ if f not in ("seq", "recv_mono_ns", "recv_wall_ns")}
            p.on_callback(ev.recv_mono_ns, ev.recv_wall_ns, type(ev), fields)

    feed(s.events)
    assert p.publisher.latest().instruments[0].tape.has_quote
    n = len(s.events)
    s.request("reqTickByTickData", 30_002, tick_type="BidAsk")     # BBO resubscribed: old quotes invalid
    feed(s.events[n:])
    t = p.publisher.latest().instruments[0].tape
    assert not t.has_quote and not t.context_ok


# ---------------------------------------------------------------------------
# Deliberately reordered BidAsk / AllLast streams
# ---------------------------------------------------------------------------

def test_reordered_streams_same_true_event():
    """True order: quote Q, BUY print at Q.ask lifts the level, new quote Q'. Both delivery
    orders must label the print BUY (direct when in order, quote-history when Q' overtakes it)."""
    results = []
    for trade_first in (True, False):
        h, s = ready()
        step(h, s, s.bbo, BBO, BASE, BASE + TICK)
        s.advance(1)
        if trade_first:
            step(h, s, s.trade, TRADES, BASE + TICK, 2)
            step(h, s, s.bbo, BBO, BASE + TICK, BASE + 2 * TICK)
        else:
            step(h, s, s.bbo, BBO, BASE + TICK, BASE + 2 * TICK)
            step(h, s, s.trade, TRADES, BASE + TICK, 2)
        results.append(last(h))
    assert results[0].aggressor is BUY and results[0].method is ClassMethod.DIRECT_QUOTE
    assert results[1].aggressor is BUY and results[1].method is ClassMethod.HISTORICAL_QUOTE
    assert results[1].confidence < results[0].confidence


def test_random_reordering_is_deterministic_and_totals_consistent():
    rnd = random.Random(42)
    s = RawScript().bootstrap().seed_book()
    s.advance(600)
    s.tick()
    mid = BASE
    for i in range(2000):
        s.advance(rnd.choice((0.1, 1, 5, 50)))
        if rnd.random() < 0.5:
            mid += rnd.choice((-TICK, 0, TICK))
            s.bbo(BBO, mid, mid + TICK)
        else:
            s.trade(TRADES, mid + rnd.choice((-TICK, 0, TICK, 2 * TICK)), rnd.randint(1, 5),
                    unreported=rnd.random() < 0.02)
        if i % 100 == 0:
            s.tick()
    a = Harness(tape=dataclasses.replace(TapeConfig(), max_trades=500)).run(s)
    b = Harness(tape=dataclasses.replace(TapeConfig(), max_trades=500)).run(s)
    ta, tb = a.engine.instruments[1].tape, b.engine.instruments[1].tape
    assert ta.trades() == tb.trades() and a.engine.snapshot() == b.engine.snapshot()
    assert len(ta) == 500
    check_totals(ta)
    methods = {t.method for t in ta.trades()}
    assert ClassMethod.DIRECT_QUOTE in methods and ClassMethod.NONE in methods


def test_tape_report_tool_replays_a_recording(tmp_path, capsys):
    from hermes.config import RecorderConfig
    from hermes.storage.recorder import Recorder
    from tools.tape_report import main

    s = RawScript().bootstrap().seed_book()
    s.advance(600)
    s.tick()
    s.bbo(BBO, BASE, BASE + TICK)
    s.advance(5)
    s.trade(TRADES, BASE + TICK, 2)
    s.bbo(BBO, BASE + TICK, BASE + 2 * TICK)
    s.trade(TRADES, BASE + TICK, 3)                   # quote moved up before this print
    rec = Recorder(RecorderConfig(directory=str(tmp_path), flush_interval_ms=5), {}, session_id="t")
    rec.start()
    for ev in s.events:
        rec.submit(ev)
    rec.stop()
    assert main([str(rec.session_dir), "--window-ms", "0", "250"]) == 0
    out = capsys.readouterr().out
    assert "ambiguity_window_ms=0: 2 trades" in out and "ambiguity_window_ms=250: 2 trades" in out
    assert "historical_quote=1" in out                # only with the 250 ms window
    assert "historical quote age ms" in out
