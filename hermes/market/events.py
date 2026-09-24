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
    DEPTH = "depth"                # reqMktDepth
    TRADES = "trades"              # tick-by-tick AllLast
    BBO = "bbo"                    # tick-by-tick BidAsk (primary BBO)
    L1 = "l1"                      # reqMktData (LIVE confirmation + health cross-check only)
    CONTRACT = "contract"          # reqContractDetails / reqMarketRule
    MARKET_DATA = "market_data"    # whole market-data session (e.g. 10197, delayed data)


class StreamStatus(Enum):
    IDLE = "idle"                  # never requested
    REQUESTED = "requested"        # requested, no data yet
    ACTIVE = "active"              # data received for the active generation
    DEGRADED = "degraded"          # e.g. data farm broken / connectivity lost
    UNAVAILABLE = "unavailable"    # rejected / not subscribed / capacity exceeded / retry budget exhausted
    BLOCKED = "blocked"            # hard block (10197 session conflict, delayed/frozen data)
    CANCELLED = "cancelled"
    DISCONNECTED = "disconnected"


class SubscriptionAction(Enum):
    REQUESTED = "requested"
    CANCELLED = "cancelled"


class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECT_FAILED = "connect_failed"
    CONNECTED = "connected"                # nextValidId received
    LOST = "lost"                          # 1100
    RESTORED_DATA_LOST = "restored_lost"   # 1101
    RESTORED_DATA_KEPT = "restored_kept"   # 1102
    CLOSED = "closed"                      # connectionClosed


class ErrorClass(Enum):
    """Semantic classification of an IBKR error/status code (see hermes.ibkr.errors)."""

    CONNECTIVITY_LOST = "connectivity_lost"
    RESTORED_DATA_LOST = "restored_data_lost"
    RESTORED_DATA_KEPT = "restored_data_kept"
    SERVER_CONNECTIVITY_BROKEN = "server_connectivity_broken"
    FARM_BROKEN = "farm_broken"
    FARM_OK = "farm_ok"
    DEPTH_RESET = "depth_reset"
    DEPTH_HALTED = "depth_halted"
    SUBSCRIPTION_REJECTED = "subscription_rejected"
    CAPACITY_EXCEEDED = "capacity_exceeded"
    DATA_NOT_LIVE = "data_not_live"
    SESSION_CONFLICT = "session_conflict"
    CONTRACT_ERROR = "contract_error"
    CONNECT_FAILED = "connect_failed"
    NOT_CONNECTED = "not_connected"
    READONLY_REJECTED = "readonly_rejected"   # TWS refused because the API is read-only
    INFO = "info"
    UNKNOWN = "unknown"


class L1Field(Enum):
    BID = "bid"
    ASK = "ask"
    LAST = "last"
    BID_SIZE = "bid_size"
    ASK_SIZE = "ask_size"
    LAST_SIZE = "last_size"
    VOLUME = "volume"
    HIGH = "high"
    LOW = "low"
    CLOSE = "close"
    OPEN = "open"
    OTHER = "other"


class AnomalyKind(Enum):
    OFF_GRID_PRICE = "off_grid_price"
    NON_INTEGRAL_SIZE = "non_integral_size"
    INVALID_CODE = "invalid_code"
    UNKNOWN_REQ_ID = "unknown_req_id"
    INACTIVE_REQ_ID = "inactive_req_id"       # callback from a replaced subscription generation
    NO_PRICE_GRID = "no_price_grid"
    DELAYED_TICK = "delayed_tick"             # delayed tick type on L1 => data is not live
    CONTRACT_MISMATCH = "contract_mismatch"


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketEvent:
    seq: int
    sub: int = 0
    instrument_id: int
    recv_mono_ns: int
    recv_wall_ns: int
    # Subscription generation (the IBKR reqId of the subscription that produced the event).
    # 0 for events not tied to a subscription. The engine ignores events whose generation is not
    # the ACTIVE generation of their stream (old-callback protection).
    generation: int = 0


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
class L1TickEvent(MarketEvent):
    """reqMktData tick. Health cross-check only — never used for order-flow/aggressor logic."""

    field: L1Field
    price_units: int | None = None
    size: int | None = None
    ibkr_tick_type: int = -1


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketDataTypeEvent(MarketEvent):
    market_data_type: int       # 1 = LIVE; anything else is not live

    @property
    def is_live(self) -> bool:
        return self.market_data_type == 1


@dataclass(frozen=True, slots=True, kw_only=True)
class SubscriptionEvent(MarketEvent):
    """A subscription generation was requested or cancelled (``generation`` = its reqId)."""

    stream: Stream
    action: SubscriptionAction


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestFailedEvent(MarketEvent):
    stream: Stream | None
    method: str
    error: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorEvent(MarketEvent):
    code: int
    req_id: int
    message: str
    error_class: ErrorClass
    stream: Stream | None = None        # stream owning req_id (active or not)
    generation_active: bool = False     # req_id is the ACTIVE generation of ``stream``


@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectionEvent(MarketEvent):
    state: ConnectionState
    code: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class HeartbeatEvent(MarketEvent):
    tws_time_s: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ClockTickEvent(MarketEvent):
    due_mono_ns: int = 0        # when the tick was due; recv_mono_ns is when it was actually emitted
    coalesced: int = 1          # number of intervals covered by this tick


@dataclass(frozen=True, slots=True, kw_only=True)
class ControlEvent(MarketEvent):
    """Local supervisor/operator decisions that the engine must see (recorded, replayed)."""

    kind: str
    detail: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ContractResolvedEvent(MarketEvent):
    con_id: int
    symbol: str
    local_symbol: str
    exchange: str
    expiry: str
    multiplier: str
    min_tick: str
    market_rule_id: int
    time_zone: str = ""
    trading_hours: str = ""
    liquid_hours: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ContractFailedEvent(MarketEvent):
    reason: str


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
    stream: Stream | None = None
    detail: str = ""
