"""IBKR callback adapter and the raw-event pipeline.

Threading (decision 4): callbacks run on the official ``EClient.run()`` thread
("ibkr-dispatch"). A few callbacks can also fire on the thread calling ``connect()``
(``connectAck``, connect errors) — before/after the dispatch thread runs. Every entry goes
through ``RawPipeline``'s lock, so the pipeline and the engine are always driven by exactly one
thread at a time; the engine's owner guard enforces that nothing bypasses the pipeline.

Per callback (hot path, no I/O, no blocking):
    1. timestamp (``perf_counter_ns`` + ``time_ns``) — first statements of every callback
    2. drain local events posted by other threads (gateway requests: true send timestamps kept)
    3. emit a RawTimerTick if one is due (not a precise timer — C3 amendment C)
    4. build the RawEvent (global seq) -> Recorder.submit (non-blocking)
    5. Normalizer -> MarketEngine
    6. in-thread consumers (the live Session), then drain what they posted
    7. snapshot publication (cadence), latency observations

A callback never raises into ibapi: an exception would end ``EClient.run()`` and disconnect.
Processing errors are counted, logged, and turned into a critical engine alert (fail safe).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from decimal import Decimal
from typing import Any, Protocol

from ibapi.wrapper import EWrapper

from hermes.ibkr import raw_events as R
from hermes.ibkr.normalizer import Normalizer
from hermes.market.engine import MarketEngine
from hermes.market.events import MarketEvent, TradeEvent
from hermes.market.snapshot import SnapshotPublisher

log = logging.getLogger("hermes.pipeline")
_mono = time.perf_counter_ns
_wall = time.time_ns


class SingleWriterViolation(RuntimeError):
    pass


class RawSink(Protocol):
    def submit(self, ev: R.RawEvent) -> bool: ...


class Consumer(Protocol):
    def after_event(self, raw: R.RawEvent, events: tuple[MarketEvent, ...], now_mono_ns: int) -> None: ...


class RawPipeline:
    def __init__(self, normalizer: Normalizer, engine: MarketEngine, telemetry: Any,
                 publisher: SnapshotPublisher, recorder: RawSink | None = None,
                 tick_interval_ns: int = 250_000_000, snapshot_interval_ns: int = 100_000_000) -> None:
        self.normalizer = normalizer
        self.engine = engine
        self.telemetry = telemetry
        self.publisher = publisher
        self.recorder = recorder
        self.consumers: list[Consumer] = []
        self._lock = threading.Lock()
        self._owner: int | None = None
        self._seq = 0
        self._pending: deque[tuple[type, dict[str, Any]]] = deque()
        self._tick_ns = tick_interval_ns
        self._next_tick: int | None = None
        self._snap_ns = snapshot_interval_ns
        self._last_publish = 0
        self._last_token: tuple | None = None
        self.state_publishes = 0
        self.callbacks = 0
        self.internal_errors = 0
        self.last_callback_mono_ns = 0
        self.ever_market_data_ok = False
        self.first_ok_mono_ns: int | None = None
        engine.set_owner_guard(self._check_owner)

    # ------------------------------------------------------------------ guards
    def _check_owner(self) -> None:
        if self._owner != threading.get_ident():
            raise SingleWriterViolation("MarketEngine written outside the pipeline's current owner thread")

    @property
    def seq(self) -> int:
        return self._seq

    # ------------------------------------------------------------------ inputs
    def post_local(self, cls: type, **fields: Any) -> None:
        """Queue a local event from ANY thread; it is sequenced at the next pipeline entry.

        Callers capture their own true timestamps in ``fields`` (e.g. gateway sent_mono_ns).
        """
        self._pending.append((cls, fields))

    def on_callback(self, m: int, w: int, cls: type, fields: dict[str, Any]) -> None:
        """Entry point for every EWrapper callback. Never raises."""
        try:
            with self._lock:
                self._owner = threading.get_ident()
                try:
                    self.callbacks += 1
                    self.last_callback_mono_ns = m
                    self._drain(m, w)
                    self._maybe_tick(m, w)
                    self._seq += 1
                    self._process(cls(seq=self._seq, recv_mono_ns=m, recv_wall_ns=w, **fields), m)
                    self._drain(m, w)
                    self._publish_if_due(m)
                finally:
                    self._owner = None
            self.telemetry.observe("callback_total", _mono() - m)
        except Exception as exc:  # noqa: BLE001 - never propagate into ibapi's run loop
            self.internal_errors += 1
            log.exception("pipeline failure in %s: %s", getattr(cls, "__name__", cls), exc)

    def pump(self) -> None:
        """Sequence pending local events now (used at shutdown after the dispatch thread ended)."""
        m, w = _mono(), _wall()
        with self._lock:
            self._owner = threading.get_ident()
            try:
                self._drain(m, w)
                self._publish_if_due(m, force=True)
            finally:
                self._owner = None

    # ------------------------------------------------------------------ internals (under lock)
    def _drain(self, m: int, w: int) -> None:
        p = self._pending
        while p:
            cls, fields = p.popleft()
            self._seq += 1
            self._process(cls(seq=self._seq, recv_mono_ns=m, recv_wall_ns=w, **fields), m)

    def _maybe_tick(self, m: int, w: int) -> None:
        due = self._next_tick
        if due is None:
            self._next_tick = m + self._tick_ns
            return
        if m < due:
            return
        n = (m - due) // self._tick_ns + 1
        self._next_tick = due + n * self._tick_ns
        self._seq += 1
        self._process(R.RawTimerTick(seq=self._seq, recv_mono_ns=m, recv_wall_ns=w, due_mono_ns=due, coalesced=n), m)

    def _process(self, raw: R.RawEvent, m: int) -> None:
        tel = self.telemetry
        rec = self.recorder
        if rec is not None and not rec.submit(raw):
            tel.incr("recorder_rejected")
        try:
            t0 = _mono()
            events = self.normalizer.normalize(raw)
            t1 = _mono()
            tel.observe("normalize", t1 - t0)
            eng = self.engine
            for ev in events:
                t2 = _mono()
                eng.on_event(ev)
                dt = _mono() - t2
                tel.observe("engine_event", dt)
                if type(ev) is TradeEvent:
                    tel.observe("trade_classify_tape", dt)     # C4: classification + tape update
            tel.observe("core_total", _mono() - t0)
        except SingleWriterViolation:
            raise
        except Exception as exc:  # noqa: BLE001
            self.internal_errors += 1
            log.exception("processing failed for %s seq=%d", type(raw).__name__, raw.seq)
            self.engine.internal_error(f"{type(raw).__name__} seq={raw.seq}: {exc!r}", m)
            events = ()
        for c in self.consumers:
            try:
                c.after_event(raw, events, m)
            except Exception as exc:  # noqa: BLE001
                self.internal_errors += 1
                log.exception("consumer %s failed: %s", type(c).__name__, exc)
        if self.engine.new_alerts:
            for key in self.engine.new_alerts:
                log.critical("ALERT %s: %s", key, self.engine.alerts.get(key, ""))
            self.engine.new_alerts.clear()

    def _publish_if_due(self, m: int, force: bool = False) -> None:
        """Publish at the end of a pipeline entry (one consistent state per callback).

        A snapshot is published when (a) any health/quality state changed (``state_token``) —
        IMMEDIATELY, never deferred to the cadence — or (b) the cadence elapsed (row/price
        freshness), or (c) forced.
        """
        token = self.engine.state_token()
        changed = token != self._last_token
        if not (force or changed) and m - self._last_publish < self._snap_ns:
            return
        if changed:
            self.state_publishes += 1
        self._last_token = token
        self._last_publish = m
        snap = self.engine.snapshot()
        self.publisher.publish(snap)
        self.telemetry.observe("snapshot_publish", _mono() - m)
        if not self.ever_market_data_ok and any(i.market_data_ok for i in snap.instruments):
            self.ever_market_data_ok = True
            self.first_ok_mono_ns = m


def _contract_fields(req_id: int, cd: Any) -> dict[str, Any]:
    c = cd.contract
    return dict(
        req_id=req_id, con_id=int(c.conId), symbol=str(c.symbol), sec_type=str(c.secType),
        local_symbol=str(c.localSymbol), trading_class=str(c.tradingClass),
        last_trade_date_or_contract_month=str(c.lastTradeDateOrContractMonth), exchange=str(c.exchange),
        primary_exchange=str(c.primaryExchange), currency=str(c.currency), multiplier=str(c.multiplier),
        min_tick=float(cd.minTick), market_rule_ids=str(cd.marketRuleIds or ""),
        valid_exchanges=str(cd.validExchanges or ""), time_zone_id=str(cd.timeZoneId or ""),
        trading_hours=str(cd.tradingHours or ""), liquid_hours=str(cd.liquidHours or ""),
        real_expiration_date=str(getattr(cd, "realExpirationDate", "") or ""),
        last_trade_time=str(getattr(cd, "lastTradeTime", "") or ""),
    )


def _dec(v: Any) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


class IbkrAdapter(EWrapper):
    """EWrapper implementation: timestamp immediately, hand callback data to the pipeline."""

    def __init__(self, pipeline: RawPipeline) -> None:
        super().__init__()
        self._p = pipeline

    # ---- connection / session ----
    def connectAck(self):  # noqa: N802
        m = _mono(); w = _wall()  # noqa: E702
        self._p.on_callback(m, w, R.RawConnectAck, {})

    def nextValidId(self, orderId: int):  # noqa: N802,N803
        m = _mono(); w = _wall()  # noqa: E702
        self._p.on_callback(m, w, R.RawNextValidId, {"order_id": int(orderId)})

    def connectionClosed(self):  # noqa: N802
        m = _mono(); w = _wall()  # noqa: E702
        self._p.on_callback(m, w, R.RawConnectionClosed, {})

    def currentTime(self, time: int):  # noqa: N802,A002
        m = _mono(); w = _wall()  # noqa: E702
        self._p.on_callback(m, w, R.RawCurrentTime, {"time": int(time)})

    def error(self, reqId, errorTime, errorCode, errorString, advancedOrderRejectJson=""):  # noqa: N802,N803
        m = _mono(); w = _wall()  # noqa: E702
        self._p.on_callback(m, w, R.RawError, {
            "req_id": int(reqId), "error_time": int(errorTime or 0), "code": int(errorCode),
            "message": str(errorString), "advanced_order_reject_json": str(advancedOrderRejectJson or "")})

    # ---- contracts ----
    def contractDetails(self, reqId, contractDetails):  # noqa: N802,N803
        m = _mono(); w = _wall()  # noqa: E702
        self._p.on_callback(m, w, R.RawContractDetails, _contract_fields(int(reqId), contractDetails))

    def contractDetailsEnd(self, reqId):  # noqa: N802,N803
        m = _mono(); w = _wall()  # noqa: E702
        self._p.on_callback(m, w, R.RawContractDetailsEnd, {"req_id": int(reqId)})

    def marketRule(self, marketRuleId, priceIncrements):  # noqa: N802,N803
        m = _mono(); w = _wall()  # noqa: E702
        incs = tuple((float(pi.lowEdge), float(pi.increment)) for pi in priceIncrements)
        self._p.on_callback(m, w, R.RawMarketRule, {"market_rule_id": int(marketRuleId), "increments": incs})

    # ---- market data ----
    def updateMktDepth(self, reqId, position, operation, side, price, size):  # noqa: N802,N803
        m = _mono(); w = _wall()  # noqa: E702
        self._p.on_callback(m, w, R.RawMarketDepth, {
            "req_id": reqId, "position": position, "operation": operation, "side": side,
            "price": float(price), "size": _dec(size), "is_l2": False})

    def updateMktDepthL2(self, reqId, position, marketMaker, operation, side, price, size, isSmartDepth):  # noqa: N802,N803
        m = _mono(); w = _wall()  # noqa: E702
        self._p.on_callback(m, w, R.RawMarketDepth, {
            "req_id": reqId, "position": position, "operation": operation, "side": side,
            "price": float(price), "size": _dec(size), "is_l2": True, "market_maker": str(marketMaker or ""),
            "is_smart_depth": bool(isSmartDepth)})

    def tickByTickAllLast(self, reqId, tickType, time, price, size, tickAttribLast, exchange,  # noqa: N802,N803,A002
                          specialConditions):
        m = _mono(); w = _wall()  # noqa: E702
        self._p.on_callback(m, w, R.RawTickByTickAllLast, {
            "req_id": reqId, "tick_type": int(tickType), "time": int(time), "price": float(price),
            "size": _dec(size), "past_limit": bool(getattr(tickAttribLast, "pastLimit", False)),
            "unreported": bool(getattr(tickAttribLast, "unreported", False)), "exchange": str(exchange or ""),
            "special_conditions": str(specialConditions or "")})

    def tickByTickBidAsk(self, reqId, time, bidPrice, askPrice, bidSize, askSize, tickAttribBidAsk):  # noqa: N802,N803,A002
        m = _mono(); w = _wall()  # noqa: E702
        self._p.on_callback(m, w, R.RawTickByTickBidAsk, {
            "req_id": reqId, "time": int(time), "bid_price": float(bidPrice), "ask_price": float(askPrice),
            "bid_size": _dec(bidSize), "ask_size": _dec(askSize),
            "bid_past_low": bool(getattr(tickAttribBidAsk, "bidPastLow", False)),
            "ask_past_high": bool(getattr(tickAttribBidAsk, "askPastHigh", False))})

    def tickPrice(self, reqId, tickType, price, attrib):  # noqa: N802,N803
        m = _mono(); w = _wall()  # noqa: E702
        self._p.on_callback(m, w, R.RawTickPrice, {
            "req_id": reqId, "tick_type": int(tickType), "price": float(price),
            "past_limit": bool(getattr(attrib, "pastLimit", False)),
            "pre_open": bool(getattr(attrib, "preOpen", False))})

    def tickSize(self, reqId, tickType, size):  # noqa: N802,N803
        m = _mono(); w = _wall()  # noqa: E702
        self._p.on_callback(m, w, R.RawTickSize, {"req_id": reqId, "tick_type": int(tickType), "size": _dec(size)})

    def marketDataType(self, reqId, marketDataType):  # noqa: N802,N803
        m = _mono(); w = _wall()  # noqa: E702
        self._p.on_callback(m, w, R.RawMarketDataType, {"req_id": reqId, "market_data_type": int(marketDataType)})
