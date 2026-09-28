from __future__ import annotations

from hermes.market.classify import Aggressor, ClassMethod
from hermes.market.events import BookSide
from hermes.market.orderbook import BookSnapshot, BookState, LevelChange
from hermes.market.structure import StructureEngine
from hermes.market.tape import ClassifiedTrade


def book(bids, asks, state=BookState.VALID):
    return BookSnapshot(
        instrument_id=1,
        state=state,
        issues=frozenset(),
        epoch=0,
        bids=tuple(bids),
        asks=tuple(asks),
        last_update_ns=0,
        needs_resync=False,
        stale_reason=None,
    )


def trade(t_ns, side, price, size=1, eligible=True):
    return ClassifiedTrade(
        instrument_id=1,
        seq=t_ns // 1_000_000 + 1,
        generation=1,
        tape_epoch=0,
        exch_ts_s=0,
        recv_mono_ns=t_ns,
        recv_wall_ns=t_ns,
        price_units=price,
        size=size,
        exchange="CME",
        special_conditions="",
        past_limit=False,
        unreported=False,
        eligible=eligible,
        aggressor=side,
        method=ClassMethod.DIRECT_QUOTE,
        confidence=1.0 if side is not Aggressor.UNKNOWN else 0.0,
        unknown_reason=None,
        quote_bid_units=100,
        quote_ask_units=101,
        quote_seq=None,
        ref_quote_seq=None,
        book_valid=True,
        ref_quote_age_ns=None,
    )


def seed(e, t=0):
    b = book([(100, 10), (99, 8)], [(101, 10), (102, 8)])
    e.observe_book(b, (), t)
    return b


def test_valid_book_seeds_persistence_without_counting_fake_adds():
    e = StructureEngine(1)
    seed(e, 1_000_000_000)
    e.advance(1_500_000_000)
    s = e.snapshot()
    assert s.available and s.continuity_epoch == 0
    assert len(s.current_levels) == 4
    assert all(x.age_ns == 500_000_000 for x in s.current_levels)
    assert s.windows[0].added_bid == 0
    assert s.windows[0].added_ask == 0


def test_visible_size_add_and_remove_are_measured_not_called_cancels():
    e = StructureEngine(1)
    seed(e)
    e.observe_book(
        book([(100, 15), (99, 8)], [(101, 7), (102, 8)]),
        (
            LevelChange(BookSide.BID, 100, 10, 15),
            LevelChange(BookSide.ASK, 101, 10, 7),
        ),
        100_000_000,
    )
    w = e.snapshot().windows[0]
    assert w.added_bid == 5 and w.removed_bid == 0
    assert w.added_ask == 0 and w.removed_ask == 3
    assert w.net_bid_displayed == 5
    assert w.net_ask_displayed == -3


def test_window_edge_change_is_visibility_only_not_liquidity_flow():
    e = StructureEngine(1)
    seed(e)
    e.observe_book(
        book([(100, 10), (99, 8)], [(101, 10), (103, 6)]),
        (
            LevelChange(BookSide.ASK, 102, 8, 0, at_window_edge=True),
            LevelChange(BookSide.ASK, 103, 0, 6, at_window_edge=True),
        ),
        100_000_000,
    )
    w = e.snapshot().windows[0]
    assert w.added_ask == 0 and w.removed_ask == 0
    assert w.edge_visibility_events == 2


def test_known_buy_at_best_ask_can_match_replenishment_same_price():
    e = StructureEngine(1, replenish_link_ms=250)
    b0 = seed(e)
    e.on_trade(trade(100_000_000, Aggressor.BUY, 101, 4), b0)
    b1 = book([(100, 10), (99, 8)], [(101, 14), (102, 8)])
    e.observe_book(
        b1,
        (LevelChange(BookSide.ASK, 101, 10, 14),),
        150_000_000,
    )
    w = e.snapshot().windows[0]
    assert w.known_buy_at_ask == 4
    assert w.replenished_ask == 4
    assert w.replenish_events_ask == 1


def test_replenishment_matching_is_conservative_and_consumes_hit_once():
    e = StructureEngine(1, replenish_link_ms=250)
    b0 = seed(e)
    e.on_trade(trade(10_000_000, Aggressor.SELL, 100, 3), b0)

    b1 = book([(100, 12), (99, 8)], [(101, 10), (102, 8)])
    e.observe_book(b1, (LevelChange(BookSide.BID, 100, 10, 12),), 20_000_000)

    b2 = book([(100, 15), (99, 8)], [(101, 10), (102, 8)])
    e.observe_book(b2, (LevelChange(BookSide.BID, 100, 12, 15),), 30_000_000)

    w = e.snapshot().windows[0]
    assert w.added_bid == 5
    assert w.replenished_bid == 3
    assert w.replenish_events_bid == 2


def test_trade_too_old_does_not_match_replenishment():
    e = StructureEngine(1, replenish_link_ms=100)
    b0 = seed(e)
    e.on_trade(trade(10_000_000, Aggressor.BUY, 101, 5), b0)
    e.observe_book(
        book([(100, 10), (99, 8)], [(101, 15), (102, 8)]),
        (LevelChange(BookSide.ASK, 101, 10, 15),),
        200_000_000,
    )
    assert e.snapshot().windows[0].replenished_ask == 0


def test_wrong_price_trade_is_not_linked():
    e = StructureEngine(1)
    b0 = seed(e)
    e.on_trade(trade(10_000_000, Aggressor.BUY, 102, 5), b0)
    e.observe_book(
        book([(100, 10), (99, 8)], [(101, 15), (102, 8)]),
        (LevelChange(BookSide.ASK, 101, 10, 15),),
        20_000_000,
    )
    w = e.snapshot().windows[0]
    assert w.known_buy_at_ask == 0
    assert w.replenished_ask == 0


def test_unknown_trade_is_never_redistributed_into_structure_hits():
    e = StructureEngine(1)
    b0 = seed(e)
    e.on_trade(trade(10_000_000, Aggressor.UNKNOWN, 101, 7), b0)
    e.observe_book(
        book([(100, 10), (99, 8)], [(101, 17), (102, 8)]),
        (LevelChange(BookSide.ASK, 101, 10, 17),),
        20_000_000,
    )
    w = e.snapshot().windows[0]
    assert w.known_buy_at_ask == 0
    assert w.known_sell_at_bid == 0
    assert w.replenished_total == 0


def test_level_leave_and_reenter_resets_persistence_age():
    e = StructureEngine(1)
    seed(e, 0)
    e.observe_book(
        book([(100, 10), (99, 8)], [(102, 8), (103, 5)]),
        (LevelChange(BookSide.ASK, 101, 10, 0), LevelChange(BookSide.ASK, 103, 0, 5)),
        100_000_000,
    )
    e.observe_book(
        book([(100, 10), (99, 8)], [(101, 6), (102, 8)]),
        (LevelChange(BookSide.ASK, 103, 5, 0), LevelChange(BookSide.ASK, 101, 0, 6)),
        300_000_000,
    )
    e.advance(500_000_000)
    lvl = next(x for x in e.snapshot().current_levels if x.side is BookSide.ASK and x.price_units == 101)
    assert lvl.age_ns == 200_000_000


def test_nonvalid_book_breaks_continuity_and_clears_rolling_structure():
    e = StructureEngine(1)
    b0 = seed(e)
    e.on_trade(trade(10, Aggressor.BUY, 101, 2), b0)
    e.observe_book(book([], [], BookState.SUSPECT), (), 20)
    s = e.snapshot()
    assert not s.available
    assert s.continuity_epoch == 1
    assert s.windows[2].known_buy_at_ask == 0
    assert not s.current_levels


def test_explicit_break_book_clears_state_once_and_new_valid_reseeds():
    e = StructureEngine(1)
    seed(e)
    e.break_book("depth_resubscribe", 50)
    assert e.snapshot().continuity_epoch == 1
    e.observe_book(book([(100, 5)], [(101, 5)]), (), 100)
    s = e.snapshot()
    assert s.available and s.continuity_epoch == 1
    assert len(s.current_levels) == 2


def test_rolling_windows_evict_old_flow():
    e = StructureEngine(1)
    seed(e)
    e.observe_book(
        book([(100, 15), (99, 8)], [(101, 10), (102, 8)]),
        (LevelChange(BookSide.BID, 100, 10, 15),),
        100_000_000,
    )
    e.advance(1_200_000_000)
    s = e.snapshot()
    assert s.windows[0].added_bid == 0
    assert s.windows[1].added_bid == 5
