"""Raw events: callback-level IBKR information BEFORE semantic normalization.

This is the authoritative recording / replay stream (architecture decision 5).

Rules
-----
* Fields keep IBKR semantics and IBKR numeric codes (depth side 0 = ASK / 1 = BID,
  operation 0 = insert / 1 = update / 2 = delete), prices as received (float), sizes as
  received (``decimal.Decimal``). No grid conversion, no validation, no interpretation.
* This module imports NOTHING from ``ibapi`` so recordings can be replayed without the
  TWS API installed (tested).
* ``seq`` is global and strictly increasing across ALL raw events (IBKR and local) within a
  process run; it is assigned by the adapter at callback entry.
* ``recv_mono_ns`` / ``recv_wall_ns`` are taken at callback entry (earliest point available
  without modifying ibapi internals — decision 4).

Local (non-IBKR) events that influence engine state — timer ticks, the requests we issued,
recording gaps, session markers — are also RawEvents so replay can reproduce them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class RawEvent:
    seq: int
    recv_mono_ns: int
    recv_wall_ns: int


# ---------------------------------------------------------------------------
# IBKR callback events
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, kw_only=True)
class RawIbkrEvent(RawEvent):
    """Base for events originating from an EWrapper callback."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RawMarketDepth(RawIbkrEvent):
    """``updateMktDepth`` (is_l2=False) or ``updateMktDepthL2`` (is_l2=True)."""

    req_id: int
    position: int
    operation: int              # IBKR: 0 insert, 1 update, 2 delete
    side: int                   # IBKR: 0 ASK, 1 BID
    price: float
    size: Decimal
    is_l2: bool
    market_maker: str = ""
    is_smart_depth: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class RawTickByTickAllLast(RawIbkrEvent):
    req_id: int
    tick_type: int              # IBKR: 1 = Last, 2 = AllLast
    time: int                   # epoch SECONDS (IBKR resolution)
    price: float
    size: Decimal
    past_limit: bool
    unreported: bool
    exchange: str
    special_conditions: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RawTickByTickBidAsk(RawIbkrEvent):
    req_id: int
    time: int                   # epoch SECONDS (IBKR resolution)
    bid_price: float
    ask_price: float
    bid_size: Decimal
    ask_size: Decimal
    bid_past_low: bool
    ask_past_high: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class RawContractDetails(RawIbkrEvent):
    """Selected ContractDetails fields (plain values, no ibapi objects)."""

    req_id: int
    con_id: int
    symbol: str
    sec_type: str
    local_symbol: str
    trading_class: str
    last_trade_date_or_contract_month: str
    exchange: str
    primary_exchange: str
    currency: str
    multiplier: str
    min_tick: float
    market_rule_ids: str        # comma separated, positionally aligned with valid_exchanges
    valid_exchanges: str        # comma separated
    time_zone_id: str
    trading_hours: str
    liquid_hours: str
    real_expiration_date: str = ""
    last_trade_time: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class RawContractDetailsEnd(RawIbkrEvent):
    req_id: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RawMarketRule(RawIbkrEvent):
    market_rule_id: int
    # (low_edge, increment) pairs exactly as received
    increments: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class RawError(RawIbkrEvent):
    req_id: int
    error_time: int
    code: int
    message: str
    advanced_order_reject_json: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class RawMarketDataType(RawIbkrEvent):
    req_id: int
    market_data_type: int       # IBKR: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen


@dataclass(frozen=True, slots=True, kw_only=True)
class RawCurrentTime(RawIbkrEvent):
    time: int                   # epoch seconds (TWS)


@dataclass(frozen=True, slots=True, kw_only=True)
class RawNextValidId(RawIbkrEvent):
    """Recorded because it marks API readiness. Hermès never uses the id for orders in Phase C."""

    order_id: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RawConnectAck(RawIbkrEvent):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class RawConnectionClosed(RawIbkrEvent):
    pass


# ---------------------------------------------------------------------------
# Local control events (recorded, replayed)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, kw_only=True)
class RawLocalEvent(RawEvent):
    """Base for events generated locally that influence engine state."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RawTimerTick(RawLocalEvent):
    """Emitted by the adapter at callback entry when a timer interval has elapsed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RawRequestIssued(RawLocalEvent):
    """Every subscription / cancel / query sent to TWS (allowlisted requests only)."""

    method: str
    req_id: int | None
    params: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class RawRecordingGap(RawLocalEvent):
    """Recorder overflow: raw events first_seq..last_seq (inclusive) were NOT recorded.

    The recording is not replay-complete from ``first_seq`` onward.
    """

    first_seq: int
    last_seq: int
    count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RawSessionMarker(RawLocalEvent):
    kind: str                   # "start" | "stop"
    detail: tuple[tuple[str, str], ...] = ()
