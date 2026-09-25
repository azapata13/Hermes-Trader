from __future__ import annotations

from hermes.market.classify import Aggressor, ClassMethod
from hermes.market.metrics import MetricsEngine, book_metrics
from hermes.market.orderbook import BookSnapshot, BookState
from hermes.market.tape import ClassifiedTrade


def book(bids, asks, state=BookState.VALID):
    return BookSnapshot(
        instrument_id=1, state=state, issues=frozenset(), epoch=0,
        bids=tuple(bids), asks=tuple(asks), last_update_ns=0,
        needs_resync=False, stale_reason=None,
    )


def trade(t_ns, side, size=1, price=100):
    return ClassifiedTrade(
        instrument_id=1, seq=t_ns // 1_000_000 + 1, generation=1, tape_epoch=0,
        exch_ts_s=0, recv_mono_ns=t_ns, recv_wall_ns=t_ns, price_units=price, size=size,
        exchange="CME", special_conditions="", past_limit=False, unreported=False,
        eligible=True, aggressor=side, method=ClassMethod.DIRECT_QUOTE,
        confidence=1.0 if side is not Aggressor.UNKNOWN else 0.0,
        unknown_reason=None, quote_bid_units=99, quote_ask_units=100, quote_seq=None,
        ref_quote_seq=None, book_valid=True, ref_quote_age_ns=None,
    )


def test_symmetric_book_and_depth_availability():
    b = book([(100, 10), (99, 10), (98, 10), (97, 10), (96, 10),
              (95, 10), (94, 10), (93, 10), (92, 10), (91, 10)],
             [(101, 10), (102, 10), (103, 10), (104, 10), (105, 10),
              (106, 10), (107, 10), (108, 10), (109, 10), (110, 10)])
    m = book_metrics(b, 3)
    assert m.available and m.spread_units == 1 and m.mid_x2 == 201
    assert m.l1.value == 0 and m.l3.value == 0 and m.l5.value == 0 and m.l10.value == 0
    assert m.weighted.value == 0 and m.continuity_epoch == 3


def test_bid_heavy_ask_heavy_and_partial_depth():
    b = book([(100, 30), (99, 20), (98, 10)], [(101, 10), (102, 10), (103, 10)])
    m = book_metrics(b, 0)
    assert m.l1.value == 0.5 and m.l3.value > 0
    assert m.l5.value is None and not m.l5.available
    assert m.l5.bid_levels == 3 and m.l5.ask_levels == 3
    assert book_metrics(book([(100, 5)], [(101, 15)]), 0).l1.value == -0.5


def test_zero_denominator_unavailable():
    m = book_metrics(book([(100, 0)], [(101, 0)]), 0)
    assert m.l1.value is None and not m.l1.available
    assert m.microprice_units is None


def test_microprice_direction_and_math():
    m = book_metrics(book([(100, 30)], [(101, 10)]), 0)
    assert m.microprice_units == 100.75
    assert m.micro_offset_units == 0.25


def test_book_not_valid_is_unavailable():
    m = book_metrics(book([(100, 10)], [(101, 10)], BookState.SUSPECT), 0)
    assert not m.available and m.reason == "book_suspect"


def test_ofi_bid_size_and_price_changes():
    e = MetricsEngine(1)
    e.observe_book(book([(100, 10)], [(101, 10)]), 0)
    e.observe_book(book([(100, 15)], [(101, 10)]), 100_000_000, depth_event=True)
    assert e.snapshot().event_ofi == 5
    e.observe_book(book([(101, 7)], [(102, 9)]), 200_000_000, depth_event=True)
    assert e.snapshot().event_ofi == 17


def test_ofi_ask_size_changes():
    e = MetricsEngine(1)
    e.observe_book(book([(100, 10)], [(101, 10)]), 0)
    e.observe_book(book([(100, 10)], [(101, 14)]), 100_000_000, depth_event=True)
    assert e.snapshot().event_ofi == -4


def test_trade_flow_unknown_stays_separate():
    e = MetricsEngine(1)
    e.on_trade(trade(100_000_000, Aggressor.BUY, 5))
    e.on_trade(trade(200_000_000, Aggressor.SELL, 2))
    e.on_trade(trade(300_000_000, Aggressor.UNKNOWN, 7))
    f = e.snapshot().trade_flow[0]
    assert (f.buy_volume, f.sell_volume, f.unknown_volume) == (5, 2, 7)
    assert f.known_delta == 3 and f.known_volume_ratio == 0.5


def test_rolling_window_eviction_and_velocity():
    e = MetricsEngine(1)
    e.on_trade(trade(0, Aggressor.BUY, 2))
    e.on_trade(trade(900_000_000, Aggressor.SELL, 3))
    e.on_bbo(900_000_000)
    e.observe_book(book([(100, 10)], [(101, 10)]), 900_000_000, depth_event=True)
    e.advance(1_100_000_000)
    s = e.snapshot()
    assert (s.trade_flow[0].buy_volume, s.trade_flow[0].sell_volume) == (0, 3)
    v = s.velocity[0]
    assert v.trades == 1 and v.contracts == 3 and v.depth_updates == 1 and v.bbo_updates == 1


def test_price_movement_requires_anchor():
    e = MetricsEngine(1)
    e.observe_book(book([(100, 10)], [(101, 10)]), 0)
    e.on_trade(trade(0, Aggressor.BUY, 1, 101))
    e.advance(1_100_000_000)
    e.observe_book(book([(101, 10)], [(102, 10)]), 1_100_000_000, depth_event=True)
    e.on_trade(trade(1_100_000_000, Aggressor.BUY, 1, 103))
    p1 = e.snapshot().price_move[0]
    assert p1.midpoint_x2_change == 2 and p1.last_trade_change == 2


def test_continuity_break_resets_windows():
    e = MetricsEngine(1)
    e.observe_book(book([(100, 10)], [(101, 10)]), 0)
    e.observe_book(book([(100, 12)], [(101, 10)]), 10, depth_event=True)
    e.on_trade(trade(20, Aggressor.BUY, 4))
    e.break_all("disconnect", 30)
    s = e.snapshot()
    assert s.continuity_epoch == 1
    assert s.ofi[2].events == 0 and s.trade_flow[2].total_volume == 0
    assert not s.book.available


def test_invalid_period_does_not_bridge_ofi():
    e = MetricsEngine(1)
    e.observe_book(book([(100, 10)], [(101, 10)]), 0)
    e.observe_book(book([(100, 12)], [(101, 10)]), 10, depth_event=True)
    e.observe_book(book([], [], BookState.BUILDING), 20)
    e.observe_book(book([(101, 10)], [(102, 10)]), 30)
    e.observe_book(book([(101, 11)], [(102, 10)]), 40, depth_event=True)
    s = e.snapshot()
    assert s.continuity_epoch == 0
    assert s.ofi[2].events == 1 and s.event_ofi == 1
