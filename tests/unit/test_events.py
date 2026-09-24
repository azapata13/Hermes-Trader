from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from hermes.ibkr import codes
from hermes.ibkr.raw_events import RawEvent, RawIbkrEvent, RawLocalEvent, RawMarketDepth, RawRecordingGap, RawTimerTick
from hermes.market.events import BookSide, DepthOp, DepthRowEvent, MarketEvent, TradeEvent


def _depth_event() -> DepthRowEvent:
    return DepthRowEvent(seq=7, instrument_id=1, recv_mono_ns=100, recv_wall_ns=200,
                         side=BookSide.BID, op=DepthOp.INSERT, position=0, price_units=84939, size=12)


def test_market_events_are_frozen_slotted_kw_only():
    ev = _depth_event()
    assert isinstance(ev, MarketEvent) and ev.sub == 0
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.size = 1  # type: ignore[misc]
    assert not hasattr(ev, "__dict__")
    with pytest.raises(TypeError):
        DepthRowEvent(7, 0, 1, 100, 200, BookSide.BID, DepthOp.INSERT, 0, 1, 1)  # type: ignore[misc]


def test_event_equality_is_value_based():
    assert _depth_event() == _depth_event()
    t = TradeEvent(seq=1, instrument_id=1, recv_mono_ns=1, recv_wall_ns=1, price_units=4, size=1, exch_ts_s=0)
    assert t.exchange == "" and t.past_limit is False


def test_raw_events_hierarchy_and_immutability():
    raw = RawMarketDepth(seq=1, recv_mono_ns=5, recv_wall_ns=6, req_id=4001, position=0, operation=0,
                         side=1, price=21234.75, size=Decimal("3"), is_l2=False)
    assert isinstance(raw, RawIbkrEvent) and isinstance(raw, RawEvent)
    assert isinstance(RawTimerTick(seq=2, recv_mono_ns=1, recv_wall_ns=1), RawLocalEvent)
    gap = RawRecordingGap(seq=3, recv_mono_ns=1, recv_wall_ns=1, first_seq=10, last_seq=19, count=10)
    assert gap.count == 10
    with pytest.raises(dataclasses.FrozenInstanceError):
        raw.price = 1.0  # type: ignore[misc]


def test_ibkr_code_mapping():
    # IBKR: side 0 = ASK, 1 = BID ; op 0 insert, 1 update, 2 delete
    assert codes.depth_side(0) is BookSide.ASK
    assert codes.depth_side(1) is BookSide.BID
    assert codes.depth_side(2) is None
    assert codes.depth_op(0) is DepthOp.INSERT
    assert codes.depth_op(1) is DepthOp.UPDATE
    assert codes.depth_op(2) is DepthOp.DELETE
    assert codes.depth_op(3) is None
    # internal enums deliberately do not share IBKR numeric values
    assert int(BookSide.ASK) != 0 and int(DepthOp.INSERT) != 0


def test_forbidden_msg_id_helpers():
    assert codes.is_forbidden_msg_id(3) and codes.is_forbidden_msg_id(203)
    assert not codes.is_forbidden_msg_id(10)      # REQ_MKT_DEPTH
    assert codes.base_msg_id(203) == 3 and codes.base_msg_id(200) == 200
