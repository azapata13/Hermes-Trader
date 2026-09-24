"""Normalizer: RawEvent -> MarketEvent(s). Pure and deterministic given the raw stream.

Responsibilities
----------------
* Subscription generations (C3 amendment A): every subscription request carries a fresh
  reqId. ``RawRequestIssued`` for a subscription makes that reqId the ACTIVE generation of
  its (instrument, stream); the previous reqId becomes inactive. Callbacks from inactive
  reqIds are still recorded (raw) but normalize to ``DataAnomalyEvent(INACTIVE_REQ_ID)`` only —
  they can never mutate market state. (The engine re-checks generations as a second layer.)
* IBKR code mapping (depth side/op), Decimal -> int sizes, float -> grid units via PriceGrid.
* Contract resolution and PriceGrid initialization from the recorded contract details and
  market rule (so replay reproduces them).
* Error classification (``hermes.ibkr.errors``) with reqId -> stream routing.

The normalizer never reads clocks and never raises on bad data: anomalies become
``DataAnomalyEvent``s that the engine handles fail-safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from hermes.ibkr import codes
from hermes.ibkr import raw_events as R
from hermes.ibkr.contracts import (
    ContractResolutionError,
    ContractSpec,
    PendingContract,
    ResolvedContract,
    build_price_grid,
)
from hermes.ibkr.errors import classify_error
from hermes.market import events as M
from hermes.market.events import AnomalyKind, ErrorClass, L1Field, Stream
from hermes.market.pricegrid import OffGridPriceError, PriceGrid

NORMALIZER_VERSION = 1

_SUBSCRIBE_METHODS = {"reqMktDepth": Stream.DEPTH, "reqMktData": Stream.L1}
_CANCEL_METHODS = {"cancelMktDepth", "cancelTickByTickData", "cancelMktData"}
_TBT_STREAM = {"AllLast": Stream.TRADES, "BidAsk": Stream.BBO}

_L1_PRICE_FIELDS = {
    codes.TICK_BID: L1Field.BID, codes.TICK_ASK: L1Field.ASK, codes.TICK_LAST: L1Field.LAST,
    codes.TICK_HIGH: L1Field.HIGH, codes.TICK_LOW: L1Field.LOW, codes.TICK_CLOSE: L1Field.CLOSE,
    codes.TICK_OPEN: L1Field.OPEN,
}
_L1_SIZE_FIELDS = {
    codes.TICK_BID_SIZE: L1Field.BID_SIZE, codes.TICK_ASK_SIZE: L1Field.ASK_SIZE,
    codes.TICK_LAST_SIZE: L1Field.LAST_SIZE, codes.TICK_VOLUME: L1Field.VOLUME,
}


@dataclass(frozen=True, slots=True)
class Route:
    instrument_id: int
    stream: Stream
    method: str


def _int_size(size: Decimal | int | float) -> int | None:
    try:
        d = size if isinstance(size, Decimal) else Decimal(str(size))
        if not d.is_finite() or d != d.to_integral_value():
            return None
        return int(d)
    except Exception:  # noqa: BLE001 - never raise on bad data
        return None


class Normalizer:
    def __init__(self) -> None:
        self._routes: dict[int, Route] = {}
        self._active: dict[tuple[int, Stream], int] = {}
        self._grids: dict[int, PriceGrid] = {}
        self._pending_contracts: dict[int, PendingContract] = {}
        self._pending_rules: dict[int, list[tuple[int, ResolvedContract]]] = {}
        self._resolved: dict[int, ResolvedContract] = {}
        self._dispatch: dict[type, Callable[[R.RawEvent], tuple[M.MarketEvent, ...]]] = {
            R.RawMarketDepth: self._depth,
            R.RawTickByTickAllLast: self._trade,
            R.RawTickByTickBidAsk: self._bbo,
            R.RawTickPrice: self._tick_price,
            R.RawTickSize: self._tick_size,
            R.RawMarketDataType: self._market_data_type,
            R.RawError: self._error,
            R.RawContractDetails: self._contract_details,
            R.RawContractDetailsEnd: self._contract_details_end,
            R.RawMarketRule: self._market_rule,
            R.RawRequestIssued: self._request_issued,
            R.RawRequestFailed: self._request_failed,
            R.RawNextValidId: self._next_valid_id,
            R.RawConnectionClosed: self._connection_closed,
            R.RawCurrentTime: self._current_time,
            R.RawTimerTick: self._timer,
            R.RawControl: self._control,
        }

    # ------------------------------------------------------------------ public
    def normalize(self, raw: R.RawEvent) -> tuple[M.MarketEvent, ...]:
        fn = self._dispatch.get(type(raw))
        if fn is None:
            return ()   # RawConnectAck, RawSessionMarker: nothing to normalize
        return fn(raw)

    def active_generation(self, instrument_id: int, stream: Stream) -> int | None:
        return self._active.get((instrument_id, stream))

    def price_grid(self, instrument_id: int) -> PriceGrid | None:
        return self._grids.get(instrument_id)

    def resolved_contract(self, instrument_id: int) -> ResolvedContract | None:
        return self._resolved.get(instrument_id)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _hdr(raw: R.RawEvent, instrument_id: int = 0, generation: int = 0, sub: int = 0) -> dict:
        return {"seq": raw.seq, "sub": sub, "instrument_id": instrument_id,
                "recv_mono_ns": raw.recv_mono_ns, "recv_wall_ns": raw.recv_wall_ns,
                "generation": generation}

    def _anomaly(self, raw: R.RawEvent, kind: AnomalyKind, stream: Stream | None, detail: str,
                 instrument_id: int = 0, generation: int = 0) -> tuple[M.MarketEvent, ...]:
        return (M.DataAnomalyEvent(**self._hdr(raw, instrument_id, generation), kind=kind,
                                   stream=stream, detail=detail),)

    def _route_active(self, raw: R.RawEvent, req_id: int, expected: Stream
                      ) -> tuple[Route | None, tuple[M.MarketEvent, ...]]:
        """Return the route if ``req_id`` is the ACTIVE generation of ``expected``; else anomaly."""
        route = self._routes.get(req_id)
        if route is None or route.stream is not expected:
            return None, self._anomaly(raw, AnomalyKind.UNKNOWN_REQ_ID, expected, f"reqId {req_id}",
                                       generation=req_id)
        if self._active.get((route.instrument_id, route.stream)) != req_id:
            return None, self._anomaly(raw, AnomalyKind.INACTIVE_REQ_ID, route.stream,
                                       f"reqId {req_id} is not the active generation",
                                       route.instrument_id, req_id)
        return route, ()

    # ------------------------------------------------------------------ market data
    def _depth(self, raw: R.RawMarketDepth) -> tuple[M.MarketEvent, ...]:  # type: ignore[override]
        route, bad = self._route_active(raw, raw.req_id, Stream.DEPTH)
        if route is None:
            return bad
        iid, gen = route.instrument_id, raw.req_id
        side = codes.depth_side(raw.side)
        op = codes.depth_op(raw.operation)
        if side is None or op is None:
            return self._anomaly(raw, AnomalyKind.INVALID_CODE, Stream.DEPTH,
                                 f"side={raw.side} op={raw.operation}", iid, gen)
        size = _int_size(raw.size)
        if size is None:
            return self._anomaly(raw, AnomalyKind.NON_INTEGRAL_SIZE, Stream.DEPTH, str(raw.size), iid, gen)
        grid = self._grids.get(iid)
        if grid is None:
            return self._anomaly(raw, AnomalyKind.NO_PRICE_GRID, Stream.DEPTH, "", iid, gen)
        if op is M.DepthOp.DELETE:
            price_units = grid.to_units_or_none(raw.price) or 0   # price is irrelevant for deletes
        else:
            try:
                price_units = grid.to_units(raw.price)
            except OffGridPriceError as exc:
                return self._anomaly(raw, AnomalyKind.OFF_GRID_PRICE, Stream.DEPTH, str(exc), iid, gen)
        return (M.DepthRowEvent(**self._hdr(raw, iid, gen), side=side, op=op, position=raw.position,
                                price_units=price_units, size=size),)

    def _trade(self, raw: R.RawTickByTickAllLast) -> tuple[M.MarketEvent, ...]:  # type: ignore[override]
        route, bad = self._route_active(raw, raw.req_id, Stream.TRADES)
        if route is None:
            return bad
        iid, gen = route.instrument_id, raw.req_id
        grid = self._grids.get(iid)
        if grid is None:
            return self._anomaly(raw, AnomalyKind.NO_PRICE_GRID, Stream.TRADES, "", iid, gen)
        size = _int_size(raw.size)
        if size is None:
            return self._anomaly(raw, AnomalyKind.NON_INTEGRAL_SIZE, Stream.TRADES, str(raw.size), iid, gen)
        try:
            price_units = grid.to_units(raw.price)
        except OffGridPriceError as exc:
            return self._anomaly(raw, AnomalyKind.OFF_GRID_PRICE, Stream.TRADES, str(exc), iid, gen)
        return (M.TradeEvent(**self._hdr(raw, iid, gen), price_units=price_units, size=size,
                             exch_ts_s=raw.time, exchange=raw.exchange,
                             special_conditions=raw.special_conditions, past_limit=raw.past_limit,
                             unreported=raw.unreported),)

    def _bbo(self, raw: R.RawTickByTickBidAsk) -> tuple[M.MarketEvent, ...]:  # type: ignore[override]
        route, bad = self._route_active(raw, raw.req_id, Stream.BBO)
        if route is None:
            return bad
        iid, gen = route.instrument_id, raw.req_id
        grid = self._grids.get(iid)
        if grid is None:
            return self._anomaly(raw, AnomalyKind.NO_PRICE_GRID, Stream.BBO, "", iid, gen)
        out: list[M.MarketEvent] = []
        bid = grid.to_units_or_none(raw.bid_price) if raw.bid_price > 0 else None
        ask = grid.to_units_or_none(raw.ask_price) if raw.ask_price > 0 else None
        if (raw.bid_price > 0 and bid is None) or (raw.ask_price > 0 and ask is None):
            out.append(M.DataAnomalyEvent(**self._hdr(raw, iid, gen, sub=1), kind=AnomalyKind.OFF_GRID_PRICE,
                                          stream=Stream.BBO, detail=f"{raw.bid_price}/{raw.ask_price}"))
        bid_size = _int_size(raw.bid_size)
        ask_size = _int_size(raw.ask_size)
        out.insert(0, M.BboEvent(**self._hdr(raw, iid, gen), bid_units=bid, ask_units=ask,
                                 bid_size=bid_size if bid_size is not None else 0,
                                 ask_size=ask_size if ask_size is not None else 0, exch_ts_s=raw.time))
        return tuple(out)

    def _tick_price(self, raw: R.RawTickPrice) -> tuple[M.MarketEvent, ...]:  # type: ignore[override]
        route, bad = self._route_active(raw, raw.req_id, Stream.L1)
        if route is None:
            return bad
        iid, gen = route.instrument_id, raw.req_id
        if raw.tick_type in codes.DELAYED_TICK_TYPES:
            return self._anomaly(raw, AnomalyKind.DELAYED_TICK, Stream.L1, f"tickType {raw.tick_type}", iid, gen)
        grid = self._grids.get(iid)
        units = grid.to_units_or_none(raw.price) if (grid is not None and raw.price > 0) else None
        fld = _L1_PRICE_FIELDS.get(raw.tick_type, L1Field.OTHER)
        return (M.L1TickEvent(**self._hdr(raw, iid, gen), field=fld, price_units=units,
                              ibkr_tick_type=raw.tick_type),)

    def _tick_size(self, raw: R.RawTickSize) -> tuple[M.MarketEvent, ...]:  # type: ignore[override]
        route, bad = self._route_active(raw, raw.req_id, Stream.L1)
        if route is None:
            return bad
        iid, gen = route.instrument_id, raw.req_id
        if raw.tick_type in codes.DELAYED_TICK_TYPES:
            return self._anomaly(raw, AnomalyKind.DELAYED_TICK, Stream.L1, f"tickType {raw.tick_type}", iid, gen)
        fld = _L1_SIZE_FIELDS.get(raw.tick_type, L1Field.OTHER)
        return (M.L1TickEvent(**self._hdr(raw, iid, gen), field=fld, size=_int_size(raw.size),
                              ibkr_tick_type=raw.tick_type),)

    def _market_data_type(self, raw: R.RawMarketDataType) -> tuple[M.MarketEvent, ...]:  # type: ignore[override]
        route, bad = self._route_active(raw, raw.req_id, Stream.L1)
        if route is None:
            return bad
        return (M.MarketDataTypeEvent(**self._hdr(raw, route.instrument_id, raw.req_id),
                                      market_data_type=raw.market_data_type),)

    # ------------------------------------------------------------------ errors
    def _error(self, raw: R.RawError) -> tuple[M.MarketEvent, ...]:  # type: ignore[override]
        cls = classify_error(raw.code, raw.message)
        route = self._routes.get(raw.req_id) if raw.req_id >= 0 else None
        iid = route.instrument_id if route else 0
        stream = route.stream if route else None
        active = bool(route) and self._active.get((route.instrument_id, route.stream)) == raw.req_id
        gen = raw.req_id if route else 0
        out: list[M.MarketEvent] = [M.ErrorEvent(**self._hdr(raw, iid, gen), code=raw.code, req_id=raw.req_id,
                                                 message=raw.message, error_class=cls, stream=stream,
                                                 generation_active=active)]
        if cls is ErrorClass.DEPTH_RESET and active and stream is Stream.DEPTH:
            out.append(M.DepthResetEvent(**self._hdr(raw, iid, gen, sub=1), reason=M.ResetReason.IBKR_317))
        if route is not None and route.stream is Stream.CONTRACT and raw.req_id in self._pending_contracts:
            pending = self._pending_contracts.pop(raw.req_id)
            out.append(M.ContractFailedEvent(**self._hdr(raw, pending.instrument_id, gen, sub=len(out)),
                                             reason=f"IBKR error {raw.code}: {raw.message}"))
        return tuple(out)

    # ------------------------------------------------------------------ contracts
    def _contract_details(self, raw: R.RawContractDetails) -> tuple[M.MarketEvent, ...]:  # type: ignore[override]
        pending = self._pending_contracts.get(raw.req_id)
        if pending is None:
            return self._anomaly(raw, AnomalyKind.UNKNOWN_REQ_ID, Stream.CONTRACT, f"reqId {raw.req_id}",
                                 generation=raw.req_id)
        pending.details.append(raw)
        return ()

    def _contract_details_end(self, raw: R.RawContractDetailsEnd) -> tuple[M.MarketEvent, ...]:  # type: ignore[override]
        pending = self._pending_contracts.pop(raw.req_id, None)
        if pending is None:
            return self._anomaly(raw, AnomalyKind.UNKNOWN_REQ_ID, Stream.CONTRACT, f"reqId {raw.req_id}",
                                 generation=raw.req_id)
        iid = pending.instrument_id
        try:
            c = pending.resolve()
        except ContractResolutionError as exc:
            return (M.ContractFailedEvent(**self._hdr(raw, iid, raw.req_id), reason=str(exc)),)
        self._resolved[iid] = c
        self._pending_rules.setdefault(c.market_rule_id, []).append((iid, c))
        return (M.ContractResolvedEvent(**self._hdr(raw, iid, raw.req_id), con_id=c.con_id, symbol=c.symbol,
                                        local_symbol=c.local_symbol, exchange=c.exchange, expiry=c.expiry,
                                        multiplier=c.multiplier, min_tick=repr(c.min_tick),
                                        market_rule_id=c.market_rule_id, time_zone=c.time_zone,
                                        trading_hours=c.trading_hours, liquid_hours=c.liquid_hours),)

    def _market_rule(self, raw: R.RawMarketRule) -> tuple[M.MarketEvent, ...]:  # type: ignore[override]
        waiting = self._pending_rules.pop(raw.market_rule_id, [])
        out: list[M.MarketEvent] = []
        for i, (iid, c) in enumerate(waiting):
            try:
                grid = build_price_grid(c, raw.increments)
            except ContractResolutionError as exc:
                out.append(M.ContractFailedEvent(**self._hdr(raw, iid, sub=i), reason=str(exc)))
                continue
            self._grids[iid] = grid
            out.append(M.InstrumentDefinitionEvent(**self._hdr(raw, iid, sub=i), con_id=c.con_id, symbol=c.symbol,
                                                    local_symbol=c.local_symbol, expiry=c.expiry,
                                                    multiplier=c.multiplier, price_grid=grid,
                                                    time_zone=c.time_zone, trading_hours=c.trading_hours,
                                                    liquid_hours=c.liquid_hours))
        return tuple(out)

    # ------------------------------------------------------------------ requests / generations
    @staticmethod
    def _stream_for_request(raw: R.RawRequestIssued) -> Stream | None:
        if raw.method in _SUBSCRIBE_METHODS:
            return _SUBSCRIBE_METHODS[raw.method]
        if raw.method == "reqTickByTickData":
            return _TBT_STREAM.get(dict(raw.params).get("tick_type", ""))
        if raw.method == "reqContractDetails":
            return Stream.CONTRACT
        return None

    def _request_issued(self, raw: R.RawRequestIssued) -> tuple[M.MarketEvent, ...]:  # type: ignore[override]
        iid = raw.instrument_id
        if raw.method in _CANCEL_METHODS:
            route = self._routes.get(raw.req_id) if raw.req_id is not None else None
            if route is None:
                return ()
            if self._active.get((route.instrument_id, route.stream)) == raw.req_id:
                del self._active[(route.instrument_id, route.stream)]
            return (M.SubscriptionEvent(**self._hdr(raw, route.instrument_id, raw.req_id), stream=route.stream,
                                        action=M.SubscriptionAction.CANCELLED),)
        stream = self._stream_for_request(raw)
        if stream is None or raw.req_id is None:
            return ()
        self._routes[raw.req_id] = Route(iid, stream, raw.method)
        if stream is Stream.CONTRACT:
            try:
                spec = ContractSpec.from_params(raw.params)
            except ContractResolutionError as exc:
                return (M.ContractFailedEvent(**self._hdr(raw, iid, raw.req_id), reason=str(exc)),)
            self._pending_contracts[raw.req_id] = PendingContract(iid, spec)
            return ()
        self._active[(iid, stream)] = raw.req_id          # new generation; the old one is now inactive
        return (M.SubscriptionEvent(**self._hdr(raw, iid, raw.req_id), stream=stream,
                                    action=M.SubscriptionAction.REQUESTED),)

    def _request_failed(self, raw: R.RawRequestFailed) -> tuple[M.MarketEvent, ...]:  # type: ignore[override]
        route = self._routes.get(raw.req_id) if raw.req_id is not None else None
        return (M.RequestFailedEvent(**self._hdr(raw, raw.instrument_id, raw.req_id or 0),
                                     stream=route.stream if route else None, method=raw.method,
                                     error=raw.error),)

    # ------------------------------------------------------------------ session / control
    def _next_valid_id(self, raw: R.RawNextValidId) -> tuple[M.MarketEvent, ...]:  # type: ignore[override]
        return (M.ConnectionEvent(**self._hdr(raw), state=M.ConnectionState.CONNECTED),)

    def _connection_closed(self, raw: R.RawConnectionClosed) -> tuple[M.MarketEvent, ...]:  # type: ignore[override]
        return (M.ConnectionEvent(**self._hdr(raw), state=M.ConnectionState.CLOSED),)

    def _current_time(self, raw: R.RawCurrentTime) -> tuple[M.MarketEvent, ...]:  # type: ignore[override]
        return (M.HeartbeatEvent(**self._hdr(raw), tws_time_s=raw.time),)

    def _timer(self, raw: R.RawTimerTick) -> tuple[M.MarketEvent, ...]:  # type: ignore[override]
        return (M.ClockTickEvent(**self._hdr(raw), due_mono_ns=raw.due_mono_ns, coalesced=raw.coalesced),)

    def _control(self, raw: R.RawControl) -> tuple[M.MarketEvent, ...]:  # type: ignore[override]
        return (M.ControlEvent(**self._hdr(raw), kind=raw.kind, detail=raw.detail),)
