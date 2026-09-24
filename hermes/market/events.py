"""Normalized, IBKR-independent market events (``MarketEvent``).

Produced by the Normalizer (C3) from ``RawEvent``s, consumed by the MarketEngine.
Nothing here knows about IBKR callback signatures or IBKR numeric codes; the IBKR code
mapping lives in ``hermes.ibkr.codes``.

All events are frozen, slotted, keyword-only dataclasses. Prices are integer grid units
(see ``hermes.market.pricegrid.PriceGrid``); sizes are integer contracts.

Ordering key: ``(seq, sub)`` where ``seq`` is the sequence number of the source RawEvent and
``sub`` the index of this event among the events produced from that raw event.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import NewType

InstrumentId = NewType("InstrumentId", int)


class BookSide(IntEnum):
    """Internal book side. Deliberately NOT the IBKR numeric code (IBKR: 0 = ASK, 1 = BID)."""

    BID = 1
    ASK = 2


class DepthOp(IntEnum):
    INSERT = 1
    UPDATE = 2
    DELETE = 3


class ResetReason(Enum):
    IBKR_317 = "ibkr_317"          # "Market depth data has been RESET"
    RESUBSCRIBE = "resubscribe"    # we cancelled and re-requested depth
    DISCONNECT = "disconnect"
    MANUAL = "manual"


class Stream(Enum):
    DEPTH = "depth"
    TRADES = "trades"              # tick-by-tick AllLast
    BBO = "bbo"                    # tick-by-tick BidAsk
    CONTRACT = "contract"
    MARKET_DATA = "market_data"    # whole market-data session (e.g. 10197, delayed data)


class StreamStatus(Enum):
    REQUESTED = "requested"
    ACTIVE = "active"
    DEGRADED = "degraded"          # e.g. data farm broken
    UNAVAILABLE = "unavailable"    # rejected / not subscribed / capacity exceeded
    BLOCKED = "blocked"            # hard block (10197 session conflict, delayed/frozen data)
    CANCELLED = "cancelled"


class ConnectionState(Enum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    LOST = "lost"                          # 1100 / connectionClosed
    RESTORED_DATA_LOST = "restored_lost"   # 1101
    RESTORED_DATA_KEPT = "restored_kept"   # 1102
    DISCONNECTED = "disconnected"


class AnomalyKind(Enum):
    OFF_GRID_PRICE = "off_grid_price"
    NON_INTEGRAL_SIZE = "non_integral_size"
    INVALID_CODE = "invalid_code"
    UNKNOWN_REQ_ID = "unknown_req_id"


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketEvent:
    seq: int
    sub: int = 0
    instrument_id: int
    recv_mono_ns: int
    recv_wall_ns: int


@dataclass(frozen=True, slots=True, kw_only=True)
class DepthRowEvent(MarketEvent):
    side: BookSide
    op: DepthOp
    position: int
    price_units: int
    size: int


@dataclass(frozen=True, slots=True, kw_only=True)
class DepthResetEvent(MarketEvent):
    reason: ResetReason


@dataclass(frozen=True, slots=True, kw_only=True)
class TradeEvent(MarketEvent):
    price_units: int
    size: int
    exch_ts_s: int
    exchange: str = ""
    special_conditions: str = ""
    past_limit: bool = False
    unreported: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class BboEvent(MarketEvent):
    bid_units: int | None       # None = no bid on the grid (empty side / sentinel)
    ask_units: int | None
    bid_size: int
    ask_size: int
    exch_ts_s: int


@dataclass(frozen=True, slots=True, kw_only=True)
class StreamStatusEvent(MarketEvent):
    stream: Stream
    status: StreamStatus
    code: int | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectionEvent(MarketEvent):
    state: ConnectionState
    code: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ClockTickEvent(MarketEvent):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class InstrumentDefinitionEvent(MarketEvent):
    con_id: int
    symbol: str
    local_symbol: str
    expiry: str
    multiplier: str
    # hermes.market.pricegrid.PriceGrid (typed loosely to keep this module dependency-free)
    price_grid: object
    time_zone: str = ""
    trading_hours: str = ""
    liquid_hours: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class DataAnomalyEvent(MarketEvent):
    kind: AnomalyKind
    detail: str = ""
