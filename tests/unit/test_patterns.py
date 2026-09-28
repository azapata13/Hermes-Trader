from hermes.market.classify import Aggressor, ClassMethod
from hermes.market.orderbook import BookSnapshot, BookState
from hermes.market.patterns import FollowResult, PatternEngine
from hermes.market.tape import ClassifiedTrade


def book(bid=100, ask=101, state=BookState.VALID):
    return BookSnapshot(
        instrument_id=1, state=state, issues=frozenset(), epoch=0,
        bids=((bid, 10),) if state is BookState.VALID else (),
        asks=((ask, 10),) if state is BookState.VALID else (),
        last_update_ns=0, needs_resync=False, stale_reason=None
    )


def trade(t, side, price, size=1, eligible=True):
    return ClassifiedTrade(
        instrument_id=1, seq=t // 1_000_000 + 1, generation=1, tape_epoch=0,
        exch_ts_s=0, recv_mono_ns=t, recv_wall_ns=t, price_units=price, size=size,
        exchange="CME", special_conditions="", past_limit=False, unreported=False,
        eligible=eligible, aggressor=side, method=ClassMethod.DIRECT_QUOTE,
        confidence=1.0, unknown_reason=None, quote_bid_units=100, quote_ask_units=101,
        quote_seq=None, ref_quote_seq=None, book_valid=True, ref_quote_age_ns=None
    )


def ready(**kw):
    e = PatternEngine(1, **kw)
    e.observe_book(book(), 0)
    return e


def test_valid_book_enables_pattern_measurement():
    s = ready().snapshot()
    assert s.available and s.continuity_epoch == 0


def test_buy_sweep_requires_multiple_prices():
    e = ready(sweep_gap_ms=250)
    e.on_trade(trade(10_000_000, Aggressor.BUY, 101, 2), book())
    e.on_trade(trade(20_000_000, Aggressor.BUY, 102, 3), book())
    e.advance(300_000_000)
    sw = e.snapshot().latest_sweeps[0]
    assert sw.levels == 2 and sw.volume == 5 and sw.span_units == 1


def test_sell_sweep_walks_prices_down():
    e = ready()
    e.on_trade(trade(10_000_000, Aggressor.SELL, 100, 1), book())
    e.on_trade(trade(20_000_000, Aggressor.SELL, 99, 2), book())
    e.on_trade(trade(30_000_000, Aggressor.SELL, 98, 3), book())
    e.advance(400_000_000)
    sw = e.snapshot().latest_sweeps[0]
    assert sw.aggressor is Aggressor.SELL and sw.levels == 3 and sw.volume == 6


def test_same_price_burst_is_not_sweep():
    e = ready()
    e.on_trade(trade(10_000_000, Aggressor.BUY, 101), book())
    e.on_trade(trade(20_000_000, Aggressor.BUY, 101), book())
    e.advance(400_000_000)
    assert not e.snapshot().latest_sweeps


def test_unknown_breaks_sweep():
    e = ready()
    e.on_trade(trade(10_000_000, Aggressor.BUY, 101), book())
    e.on_trade(trade(20_000_000, Aggressor.UNKNOWN, 101), book())
    e.on_trade(trade(30_000_000, Aggressor.BUY, 102), book())
    e.advance(400_000_000)
    assert not e.snapshot().latest_sweeps


def test_gap_finalizes_previous_sweep():
    e = ready(sweep_gap_ms=100)
    e.on_trade(trade(10_000_000, Aggressor.BUY, 101), book())
    e.on_trade(trade(20_000_000, Aggressor.BUY, 102), book())
    e.on_trade(trade(200_000_000, Aggressor.BUY, 103), book())
    assert len(e.snapshot().latest_sweeps) == 1


def test_buy_follow_through_after_horizon():
    e = ready(follow_horizon_ms=100)
    e.on_trade(trade(10_000_000, Aggressor.BUY, 101, 4), book())
    e.observe_book(book(101, 102), 120_000_000)
    f = e.snapshot().latest_follow[0]
    assert f.result is FollowResult.FOLLOW_THROUGH and f.favorable_mid_x2 == 2


def test_buy_no_follow_when_mid_flat():
    e = ready(follow_horizon_ms=100)
    e.on_trade(trade(10_000_000, Aggressor.BUY, 101, 4), book())
    e.observe_book(book(), 120_000_000)
    f = e.snapshot().latest_follow[0]
    assert f.result is FollowResult.NO_FOLLOW_THROUGH and f.favorable_mid_x2 == 0


def test_sell_no_follow_when_mid_moves_up():
    e = ready(follow_horizon_ms=100)
    e.on_trade(trade(10_000_000, Aggressor.SELL, 100, 3), book())
    e.observe_book(book(101, 102), 120_000_000)
    f = e.snapshot().latest_follow[0]
    assert f.result is FollowResult.NO_FOLLOW_THROUGH and f.favorable_mid_x2 == -2


def test_not_resolved_before_horizon():
    e = ready(follow_horizon_ms=100)
    e.on_trade(trade(10_000_000, Aggressor.BUY, 101), book())
    e.observe_book(book(101, 102), 50_000_000)
    assert not e.snapshot().latest_follow


def test_invalid_book_breaks_continuity():
    e = ready()
    e.on_trade(trade(10_000_000, Aggressor.BUY, 101), book())
    e.observe_book(book(state=BookState.SUSPECT), 20_000_000)
    s = e.snapshot()
    assert not s.available and s.continuity_epoch == 1
    assert not s.latest_sweeps and not s.latest_follow


def test_windows_evict_old_events():
    e = ready(sweep_gap_ms=10, follow_horizon_ms=10)
    e.on_trade(trade(1_000_000, Aggressor.BUY, 101, 2), book())
    e.on_trade(trade(2_000_000, Aggressor.BUY, 102, 3), book())
    e.observe_book(book(101, 102), 20_000_000)
    e.advance(1_100_000_000)
    w1, w5 = e.snapshot().windows[0], e.snapshot().windows[1]
    assert w1.buy_sweeps == 0 and w1.follow_events == 0
    assert w5.buy_sweeps == 1 and w5.follow_events == 2
