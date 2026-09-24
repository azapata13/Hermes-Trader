"""MarketEngine (C3 minimal): the single-writer, deterministic market state.

Consumes normalized ``MarketEvent``s only (never IBKR callbacks). No I/O, no clocks, no
randomness: all time comes from event fields, so replaying the recorded raw stream through
the same Normalizer + MarketEngine reproduces identical state.

C3 scope: order book, BBO, last trade, L1 cross-check state, stream/subscription health,
connection / farm / market-data blocks, bounded 10197 recovery, critical alerts, snapshots.
C4: bounded classified trade tape (hermes.market.tape / classify). Bars and order-flow
metrics arrive in C5+.

Single writer: an optional ``owner_guard`` callable (installed by the live pipeline) raises if
``on_event`` is called from any thread other than the current dispatch owner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from hermes.config import BookConfig, SessionConfig, SubscriptionsConfig, TapeConfig
from hermes.ibkr.errors import SUBSCRIPTION_FATAL
from hermes.market import events as M
from hermes.market.events import AnomalyKind, ConnectionState, ErrorClass, Stream, StreamStatus
from hermes.market.classify import QuoteState, TradeClassifier
from hermes.market.health import ConflictPhase, ConflictRecovery, StreamState
from hermes.market.orderbook import BookState, InvalidationReason, OrderBook
from hermes.market.pricegrid import PriceGrid
from hermes.market.tape import ClassifiedTrade, Tape, TapeSnapshot
from hermes.market.snapshot import (
    BboSnapshot,
    InstrumentSnapshot,
    MarketSnapshot,
    StreamSnapshot,
    TradeSnapshot,
)

_S = 1_000_000_000

# Market-data streams (subscriptions) tracked per instrument.
MARKET_STREAMS = (Stream.DEPTH, Stream.BBO, Stream.TRADES, Stream.L1)

_EVENT_STREAM: dict[type, Stream] = {
    M.DepthRowEvent: Stream.DEPTH,
    M.DepthResetEvent: Stream.DEPTH,
    M.BboEvent: Stream.BBO,
    M.TradeEvent: Stream.TRADES,
    M.L1TickEvent: Stream.L1,
    M.MarketDataTypeEvent: Stream.L1,
}

_DEPTH_ANOMALIES = frozenset({AnomalyKind.OFF_GRID_PRICE, AnomalyKind.INVALID_CODE,
                              AnomalyKind.NON_INTEGRAL_SIZE, AnomalyKind.NO_PRICE_GRID})

# Critical alert keys
ALERT_CONFLICT_EXHAUSTED = "market_data_conflict_retries_exhausted"
ALERT_CONTRACT_FAILED = "contract_resolution_failed"
ALERT_READONLY_REJECTED = "tws_readonly_rejection"
ALERT_DEPTH_RESYNC_EXHAUSTED = "depth_resync_budget_exhausted"
ALERT_INTERNAL_ERROR = "internal_error"
ALERT_READONLY_VIOLATION = "readonly_violation"


def required_streams(sub: SubscriptionsConfig) -> tuple[Stream, ...]:
    out = [Stream.DEPTH]
    if sub.tick_by_tick_bid_ask:
        out.append(Stream.BBO)
    if sub.tick_by_tick_all_last:
        out.append(Stream.TRADES)
    if sub.l1_market_data:
        out.append(Stream.L1)
    return tuple(out)


@dataclass(slots=True)
class EngineCounters:
    events: int = 0
    stale_generation_rejected: int = 0      # second-layer generation filter
    inactive_callbacks: int = 0             # normalizer: callbacks from replaced generations
    unknown_req_id: int = 0
    anomalies: dict[str, int] = field(default_factory=dict)
    errors_by_code: dict[int, int] = field(default_factory=dict)
    resubscribe_all_requests: int = 0
    bbo_frozen_suspect: int = 0             # trade far outside an old BBO (evidence, not a verdict)


@dataclass(slots=True)
class InstrumentState:
    instrument_id: int
    required: tuple[Stream, ...]
    streams: dict[Stream, StreamState]
    local_symbol: str = ""
    con_id: int | None = None
    contract_state: str = "pending"
    grid: PriceGrid | None = None
    book: OrderBook | None = None
    bbo: BboSnapshot | None = None
    last_trade: TradeSnapshot | None = None
    market_data_type: int | None = None     # for the active L1 generation
    mdt_generation: int | None = None
    l1: dict[M.L1Field, int] = field(default_factory=dict)
    tape: Tape | None = None
    classifier: TradeClassifier | None = None


class MarketEngine:
    def __init__(self, book_cfg: BookConfig, session_cfg: SessionConfig, sub_cfg: SubscriptionsConfig,
                 owner_guard: Callable[[], None] | None = None, tape_cfg: TapeConfig | None = None) -> None:
        self._book_cfg = book_cfg
        self._tape_cfg = tape_cfg or TapeConfig()
        self._required = required_streams(sub_cfg)
        self._owner_guard = owner_guard
        self.instruments: dict[int, InstrumentState] = {}
        self.connection = ConnectionState.DISCONNECTED
        self.farm_broken = False
        self.not_live = False
        self._not_live_seq = -1
        self.resubscribe_all_pending = False
        self._resubscribe_all_seq = -1
        self.conflict = ConflictRecovery(max_attempts=session_cfg.conflict_max_attempts,
                                         attempt_timeout_ns=int(session_cfg.recovery_attempt_timeout_s * _S))
        self.alerts: dict[str, str] = {}
        self.new_alerts: list[str] = []          # drained by the telemetry layer
        self.counters = EngineCounters()
        self.last_seq = 0
        self.last_mono_ns = 0
        self.last_wall_ns = 0
        self._handlers: dict[type, Callable] = {
            M.DepthRowEvent: self._on_depth,
            M.DepthResetEvent: self._on_depth_reset,
            M.BboEvent: self._on_bbo,
            M.TradeEvent: self._on_trade,
            M.L1TickEvent: self._on_l1,
            M.MarketDataTypeEvent: self._on_mdt,
            M.SubscriptionEvent: self._on_subscription,
            M.RequestFailedEvent: self._on_request_failed,
            M.ErrorEvent: self._on_error,
            M.ConnectionEvent: self._on_connection,
            M.HeartbeatEvent: self._on_heartbeat,
            M.ClockTickEvent: self._on_tick,
            M.ControlEvent: self._on_control,
            M.ContractResolvedEvent: self._on_contract_resolved,
            M.ContractFailedEvent: self._on_contract_failed,
            M.InstrumentDefinitionEvent: self._on_instrument,
            M.DataAnomalyEvent: self._on_anomaly,
        }

    # ================================================================== entry point
    def set_owner_guard(self, guard: Callable[[], None] | None) -> None:
        self._owner_guard = guard

    def on_event(self, ev: M.MarketEvent) -> None:
        if self._owner_guard is not None:
            self._owner_guard()
        self.counters.events += 1
        self.last_seq = ev.seq
        self.last_mono_ns = ev.recv_mono_ns
        self.last_wall_ns = ev.recv_wall_ns
        stream = _EVENT_STREAM.get(type(ev))
        if stream is not None:
            inst = self.instruments.get(ev.instrument_id)
            if inst is None or inst.streams[stream].generation != ev.generation:
                self.counters.stale_generation_rejected += 1     # old callback: never mutates state
                return
        handler = self._handlers.get(type(ev))
        if handler is not None:
            handler(ev)
        if self.conflict.phase is ConflictPhase.RECOVERING:
            self._check_recovery(ev)

    # ================================================================== instruments
    def instrument(self, instrument_id: int) -> InstrumentState:
        inst = self.instruments.get(instrument_id)
        if inst is None:
            inst = InstrumentState(instrument_id, self._required, {s: StreamState(s) for s in MARKET_STREAMS},
                                   tape=Tape(self._tape_cfg), classifier=TradeClassifier(self._tape_cfg))
            self.instruments[instrument_id] = inst
        return inst

    def _on_contract_resolved(self, ev: M.ContractResolvedEvent) -> None:
        inst = self.instrument(ev.instrument_id)
        inst.con_id = ev.con_id
        inst.local_symbol = ev.local_symbol
        inst.contract_state = "resolved"

    def _on_contract_failed(self, ev: M.ContractFailedEvent) -> None:
        inst = self.instrument(ev.instrument_id)
        inst.contract_state = "failed"
        self._alert(ALERT_CONTRACT_FAILED, ev.reason)

    def _on_instrument(self, ev: M.InstrumentDefinitionEvent) -> None:
        inst = self.instrument(ev.instrument_id)
        inst.grid = ev.price_grid  # type: ignore[assignment]
        inst.con_id = ev.con_id
        inst.local_symbol = ev.local_symbol
        inst.contract_state = "defined"
        if inst.book is None:
            inst.book = OrderBook(self._book_cfg, ev.instrument_id)

    # ================================================================== market data
    def _on_depth(self, ev: M.DepthRowEvent) -> None:
        inst = self.instruments[ev.instrument_id]
        inst.streams[Stream.DEPTH].on_data(ev.recv_mono_ns)
        if inst.book is not None:
            inst.book.apply(ev.side, ev.op, ev.position, ev.price_units, ev.size, ev.recv_mono_ns)

    def _on_depth_reset(self, ev: M.DepthResetEvent) -> None:
        inst = self.instruments[ev.instrument_id]
        if inst.book is not None:
            inst.book.reset(ev.reason, ev.recv_mono_ns)

    def _on_bbo(self, ev: M.BboEvent) -> None:
        inst = self.instruments[ev.instrument_id]
        inst.streams[Stream.BBO].on_data(ev.recv_mono_ns)
        inst.bbo = BboSnapshot(ev.bid_units, ev.ask_units, ev.bid_size, ev.ask_size, ev.exch_ts_s, ev.recv_mono_ns)
        if inst.book is not None:
            inst.book.on_bbo(ev.bid_units, ev.ask_units, ev.recv_mono_ns)
        inst.classifier.on_quote(QuoteState(ev.bid_units, ev.ask_units, ev.bid_size, ev.ask_size, ev.seq,  # type: ignore[union-attr]
                                            ev.generation, ev.recv_mono_ns, ev.recv_wall_ns, ev.exch_ts_s))

    def _on_trade(self, ev: M.TradeEvent) -> None:
        inst = self.instruments[ev.instrument_id]
        inst.streams[Stream.TRADES].on_data(ev.recv_mono_ns)
        inst.last_trade = TradeSnapshot(ev.price_units, ev.size, ev.exch_ts_s, ev.recv_mono_ns)
        ok, _ = self.classification_context(inst)
        c = inst.classifier.classify(ev.price_units, ev.size, ev.past_limit, ev.unreported,  # type: ignore[union-attr]
                                     ev.special_conditions, ev.generation, ev.recv_mono_ns, ok)
        q = c.quote
        inst.tape.append(ClassifiedTrade(  # type: ignore[union-attr]
            instrument_id=ev.instrument_id, seq=ev.seq, generation=ev.generation, tape_epoch=inst.tape.epoch,  # type: ignore[union-attr]
            exch_ts_s=ev.exch_ts_s, recv_mono_ns=ev.recv_mono_ns, recv_wall_ns=ev.recv_wall_ns,
            price_units=ev.price_units, size=ev.size, exchange=ev.exchange,
            special_conditions=ev.special_conditions, past_limit=ev.past_limit, unreported=ev.unreported,
            eligible=c.eligible, aggressor=c.aggressor, method=c.method, confidence=c.confidence,
            unknown_reason=c.reason, quote_bid_units=q.bid_units if q else None,
            quote_ask_units=q.ask_units if q else None, quote_seq=q.seq if q else None,
            ref_quote_seq=c.ref_quote_seq,
            book_valid=inst.book is not None and inst.book.state is BookState.VALID,
            ref_quote_age_ns=c.ref_quote_age_ns))
        b = inst.bbo
        # Cross-stream evidence (telemetry only): a trade >= 2 units outside a BBO that has not
        # changed for > 2 s suggests the BBO stream may be frozen. Not a verdict by itself.
        if (b is not None and b.bid_units is not None and b.ask_units is not None
                and ev.recv_mono_ns - b.recv_mono_ns > 2 * _S
                and (ev.price_units >= b.ask_units + 2 or ev.price_units <= b.bid_units - 2)):
            self.counters.bbo_frozen_suspect += 1

    def _on_l1(self, ev: M.L1TickEvent) -> None:
        inst = self.instruments[ev.instrument_id]
        inst.streams[Stream.L1].on_data(ev.recv_mono_ns)
        value = ev.price_units if ev.price_units is not None else ev.size
        if value is not None:
            inst.l1[ev.field] = value

    def _on_mdt(self, ev: M.MarketDataTypeEvent) -> None:
        inst = self.instruments[ev.instrument_id]
        inst.streams[Stream.L1].on_data(ev.recv_mono_ns)
        inst.market_data_type = ev.market_data_type
        inst.mdt_generation = ev.generation
        if ev.is_live:
            if self.not_live and ev.seq > self._not_live_seq:
                self.not_live = False
        else:
            self._set_not_live(ev.seq, ev.recv_mono_ns)

    def _set_not_live(self, seq: int, now: int) -> None:
        self.not_live = True
        self._not_live_seq = seq
        for inst in self.instruments.values():
            if inst.book is not None:
                inst.book.invalidate(InvalidationReason.DATA_NOT_LIVE, now)
        self._break_tape_continuity()

    def _break_tape_continuity(self) -> None:
        """Classifier state (quotes, tick reference) is no longer trustworthy; trades already on the
        tape are real prints and are kept, but a new tape epoch starts."""
        for inst in self.instruments.values():
            inst.classifier.reset_all()  # type: ignore[union-attr]
            inst.tape.new_epoch()  # type: ignore[union-attr]

    def classification_context(self, inst: InstrumentState) -> tuple[bool, str]:
        """Can quote-based aggressor inference run right now? (C3 health rules + BBO stream)."""
        if self.connection is not ConnectionState.CONNECTED:
            return False, f"connection:{self.connection.value}"
        if self.farm_broken:
            return False, "farm:broken"
        if self.conflict.active:
            return False, "conflict_10197"
        if self.not_live:
            return False, "data:not_live"
        st = inst.streams[Stream.BBO]
        if st.generation is None:
            return False, "bbo:not_subscribed"
        if st.error_active:
            return False, f"bbo:error_{st.last_error_code}"
        if st.status is not StreamStatus.ACTIVE:
            return False, f"bbo:{st.status.value}"
        return True, "ok"

    # ================================================================== subscriptions
    def _on_subscription(self, ev: M.SubscriptionEvent) -> None:
        inst = self.instrument(ev.instrument_id)
        st = inst.streams.get(ev.stream)
        if st is None:
            return
        if ev.action is M.SubscriptionAction.REQUESTED:
            st.on_requested(ev.generation, ev.seq, ev.recv_mono_ns)
            if ev.stream is Stream.DEPTH and inst.book is not None:
                inst.book.reset(M.ResetReason.RESUBSCRIBE, ev.recv_mono_ns)
            elif ev.stream is Stream.BBO:
                inst.bbo = None
                inst.classifier.reset_quotes()  # type: ignore[union-attr]
                if inst.book is not None:
                    inst.book.set_bbo_unavailable(ev.recv_mono_ns)
            elif ev.stream is Stream.TRADES:
                inst.classifier.reset_tick()  # type: ignore[union-attr]
                inst.tape.new_epoch()  # type: ignore[union-attr]
            elif ev.stream is Stream.L1:
                inst.market_data_type = None
                inst.mdt_generation = None
            if self.resubscribe_all_pending and all(
                    (i.streams[s].requested_seq or -1) > self._resubscribe_all_seq
                    for i in self.instruments.values() for s in i.required):
                self.resubscribe_all_pending = False
        else:
            st.on_cancelled(ev.generation)
            if ev.stream is Stream.BBO and st.generation is None:
                inst.bbo = None
                inst.classifier.reset_quotes()  # type: ignore[union-attr]
                if inst.book is not None:
                    inst.book.set_bbo_unavailable(ev.recv_mono_ns)

    def _on_request_failed(self, ev: M.RequestFailedEvent) -> None:
        if ev.stream in MARKET_STREAMS:
            inst = self.instrument(ev.instrument_id)
            st = inst.streams[ev.stream]  # type: ignore[index]
            if st.generation == ev.generation:
                st.on_fatal_error(-1)
                self._stream_unusable(inst, ev.stream, ev.recv_mono_ns)  # type: ignore[arg-type]

    def _stream_unusable(self, inst: InstrumentState, stream: Stream, now: int) -> None:
        if stream is Stream.BBO:
            inst.classifier.reset_quotes()  # type: ignore[union-attr]
        if inst.book is None:
            return
        if stream is Stream.DEPTH:
            inst.book.invalidate(InvalidationReason.SUBSCRIPTION_FAILED, now)
        elif stream is Stream.BBO:
            inst.bbo = None
            inst.book.set_bbo_unavailable(now)

    # ================================================================== errors / connection
    def _on_error(self, ev: M.ErrorEvent) -> None:
        c = self.counters.errors_by_code
        c[ev.code] = c.get(ev.code, 0) + 1
        cls = ev.error_class
        now = ev.recv_mono_ns
        if cls is ErrorClass.CONNECTIVITY_LOST:
            self.connection = ConnectionState.LOST
            self._invalidate_books(InvalidationReason.DISCONNECT, now)
            self._break_tape_continuity()
        elif cls is ErrorClass.RESTORED_DATA_KEPT:
            self.connection = ConnectionState.CONNECTED
            self.farm_broken = False
        elif cls is ErrorClass.RESTORED_DATA_LOST:
            self.connection = ConnectionState.CONNECTED
            self.farm_broken = False
            self._invalidate_books(InvalidationReason.DATA_LOST, now)
            self._break_tape_continuity()
            self.resubscribe_all_pending = True
            self._resubscribe_all_seq = ev.seq
            self.counters.resubscribe_all_requests += 1
        elif cls in (ErrorClass.FARM_BROKEN, ErrorClass.SERVER_CONNECTIVITY_BROKEN):
            self.farm_broken = True
            self._invalidate_books(InvalidationReason.DATA_LOST, now)
            self._break_tape_continuity()
        elif cls is ErrorClass.FARM_OK:
            self.farm_broken = False
        elif cls is ErrorClass.SESSION_CONFLICT:
            self.conflict.on_conflict(ev.seq)
            self._invalidate_books(InvalidationReason.SESSION_CONFLICT, now)
            self._break_tape_continuity()
            self._conflict_alert()
        elif cls is ErrorClass.DATA_NOT_LIVE:
            self._set_not_live(ev.seq, now)
        elif cls is ErrorClass.READONLY_REJECTED:
            self._alert(ALERT_READONLY_REJECTED, f"{ev.code}: {ev.message}")
        elif cls is ErrorClass.DEPTH_HALTED and ev.generation_active and ev.stream is Stream.DEPTH:
            inst = self.instruments.get(ev.instrument_id)
            if inst is not None and inst.book is not None:
                inst.book.invalidate(InvalidationReason.SUBSCRIPTION_FAILED, now)
        if ev.generation_active and ev.stream in MARKET_STREAMS and cls in SUBSCRIPTION_FATAL:
            inst = self.instruments.get(ev.instrument_id)
            if inst is not None:
                inst.streams[ev.stream].on_fatal_error(ev.code)  # type: ignore[index]
                self._stream_unusable(inst, ev.stream, now)  # type: ignore[arg-type]

    def _on_connection(self, ev: M.ConnectionEvent) -> None:
        prev = self.connection
        if ev.state is ConnectionState.CONNECTED:
            self.connection = ConnectionState.CONNECTED
            if prev in (ConnectionState.CLOSED, ConnectionState.CONNECT_FAILED, ConnectionState.LOST):
                # Meaningful connection/session change: the 10197 retry budget may reset.
                self.conflict.reset_budget()
                self._conflict_alert()
        elif ev.state is ConnectionState.CLOSED:
            self.connection = ConnectionState.CLOSED
            self._invalidate_books(InvalidationReason.DISCONNECT, ev.recv_mono_ns)
            self._break_tape_continuity()
            for inst in self.instruments.values():
                for st in inst.streams.values():
                    st.generation = None
                    if st.status is not StreamStatus.IDLE:
                        st.status = StreamStatus.DISCONNECTED
        else:
            self.connection = ev.state

    def _invalidate_books(self, reason: InvalidationReason, now: int) -> None:
        for inst in self.instruments.values():
            if inst.book is not None:
                inst.book.invalidate(reason, now)

    def _on_heartbeat(self, ev: M.HeartbeatEvent) -> None:
        pass  # liveness is tracked by telemetry (last_mono_ns); nothing to mutate

    def _on_tick(self, ev: M.ClockTickEvent) -> None:
        now = ev.recv_mono_ns
        for inst in self.instruments.values():
            if inst.book is not None:
                inst.book.evaluate(now)
            inst.tape.evict_by_age(now)  # type: ignore[union-attr]
        before = self.conflict.phase
        self.conflict.on_tick(now)
        if before is not self.conflict.phase:
            self._conflict_alert()

    def _on_control(self, ev: M.ControlEvent) -> None:
        if ev.kind == "conflict_recovery_attempt":
            self.conflict.on_attempt(ev.seq, ev.recv_mono_ns)
        elif ev.kind == "operator_retry":
            self.conflict.reset_budget()
            self._conflict_alert()
        elif ev.kind == "connect_attempt":
            self.connection = ConnectionState.CONNECTING
        elif ev.kind == "connect_failed":
            self.connection = ConnectionState.CONNECT_FAILED
        elif ev.kind in ("contract_timeout", "market_rule_timeout"):
            for inst in self.instruments.values():
                if inst.contract_state in ("pending", "resolved"):
                    inst.contract_state = "failed"
            self._alert(ALERT_CONTRACT_FAILED, f"{ev.kind} {ev.detail}".strip())
        elif ev.kind == "depth_resync_exhausted":
            self._alert(ALERT_DEPTH_RESYNC_EXHAUSTED, ev.detail or "depth resync budget exhausted")
            for inst in self.instruments.values():
                inst.streams[Stream.DEPTH].status = StreamStatus.UNAVAILABLE
        elif ev.kind == "depth_resync_budget_reset":
            self.alerts.pop(ALERT_DEPTH_RESYNC_EXHAUSTED, None)
        elif ev.kind == "internal_error":
            self._alert(ALERT_INTERNAL_ERROR, ev.detail)
        elif ev.kind == "readonly_violation":
            self._alert(ALERT_READONLY_VIOLATION, ev.detail)

    def _on_anomaly(self, ev: M.DataAnomalyEvent) -> None:
        a = self.counters.anomalies
        a[ev.kind.value] = a.get(ev.kind.value, 0) + 1
        if ev.kind is AnomalyKind.INACTIVE_REQ_ID:
            self.counters.inactive_callbacks += 1
            return
        if ev.kind is AnomalyKind.UNKNOWN_REQ_ID:
            self.counters.unknown_req_id += 1
            return
        inst = self.instruments.get(ev.instrument_id)
        if inst is None or ev.stream is None:
            return
        if ev.stream in MARKET_STREAMS and inst.streams[ev.stream].generation != ev.generation:
            return   # anomaly from a replaced generation: counted only
        if ev.stream is Stream.DEPTH and ev.kind in _DEPTH_ANOMALIES and inst.book is not None:
            inst.book.invalidate(InvalidationReason.DATA_ANOMALY, ev.recv_mono_ns)
        elif ev.kind is AnomalyKind.DELAYED_TICK:
            self._set_not_live(ev.seq, ev.recv_mono_ns)

    # ================================================================== 10197 recovery
    def _check_recovery(self, ev: M.MarketEvent) -> None:
        """Clear the conflict only when recovery is PROVEN (never because time passed)."""
        c = self.conflict
        start = c.attempt_started_seq
        if start is None or self.connection is not ConnectionState.CONNECTED or self.farm_broken:
            return
        if c.last_conflict_seq is not None and c.last_conflict_seq > start:
            return
        for inst in self.instruments.values():
            if self.recovery_blockers(inst, start):
                return
        c.on_recovered()
        self._conflict_alert()

    def recovery_blockers(self, inst: InstrumentState, attempt_seq: int) -> list[str]:
        out: list[str] = []
        for s in inst.required:
            st = inst.streams[s]
            if st.generation is None or (st.requested_seq or -1) <= attempt_seq:
                out.append(f"{s.value}:not_resubscribed")
            elif st.error_active:
                out.append(f"{s.value}:error")
            elif s in (Stream.DEPTH, Stream.BBO) and st.first_data_mono_ns is None:
                out.append(f"{s.value}:no_fresh_data")      # AllLast: no new print required
        if Stream.L1 in inst.required and not (inst.market_data_type == 1
                                                and inst.mdt_generation == inst.streams[Stream.L1].generation):
            out.append("l1:live_not_confirmed")
        if inst.book is None or inst.book.state is not BookState.VALID:
            out.append("book:not_valid")
        return out

    def _conflict_alert(self) -> None:
        if self.conflict.phase is ConflictPhase.EXHAUSTED:
            self._alert(ALERT_CONFLICT_EXHAUSTED,
                        f"10197 recovery failed {self.conflict.attempts} times; automatic retries stopped")
            for inst in self.instruments.values():
                for s in inst.required:
                    inst.streams[s].status = StreamStatus.UNAVAILABLE
        else:
            self.alerts.pop(ALERT_CONFLICT_EXHAUSTED, None)

    # ================================================================== alerts / health
    def internal_error(self, detail: str, now_mono_ns: int) -> None:
        """Called by the pipeline when processing raised. Fail safe: books STALE + critical alert.

        Not replayable by construction (the cause is a bug); replay will raise at the same point.
        """
        self._alert(ALERT_INTERNAL_ERROR, detail)
        self._invalidate_books(InvalidationReason.INTERNAL_ERROR, now_mono_ns)

    def readonly_violation(self, detail: str) -> None:
        self._alert(ALERT_READONLY_VIOLATION, detail)

    def _alert(self, key: str, detail: str) -> None:
        if key not in self.alerts:
            self.new_alerts.append(key)
        self.alerts[key] = detail

    def effective_status(self, st: StreamState) -> StreamStatus:
        if st.status in (StreamStatus.IDLE, StreamStatus.CANCELLED, StreamStatus.UNAVAILABLE,
                         StreamStatus.DISCONNECTED):
            return st.status
        if self.conflict.active or self.not_live:
            return StreamStatus.BLOCKED
        if self.connection is not ConnectionState.CONNECTED:
            return StreamStatus.DISCONNECTED
        if self.farm_broken:
            return StreamStatus.DEGRADED
        return st.status

    def market_data_reasons(self, inst: InstrumentState) -> list[str]:
        """Empty list == market data usable. Evidence-based; silence alone never appears here."""
        r: list[str] = []
        if self.connection is not ConnectionState.CONNECTED:
            r.append(f"connection:{self.connection.value}")
        if self.farm_broken:
            r.append("farm:broken")
        if self.conflict.active:
            r.append(f"conflict_10197:{self.conflict.phase.value}")
        if self.not_live:
            r.append("data:not_live")
        if inst.contract_state != "defined":
            r.append(f"contract:{inst.contract_state}")
        for s in inst.required:
            st = inst.streams[s]
            if st.generation is None:
                r.append(f"{s.value}:not_subscribed")
            elif st.error_active:
                r.append(f"{s.value}:error_{st.last_error_code}")
            elif s is not Stream.TRADES and st.status is not StreamStatus.ACTIVE:
                r.append(f"{s.value}:{st.status.value}")
        if Stream.L1 in inst.required and not (inst.market_data_type == 1
                                                and inst.mdt_generation == inst.streams[Stream.L1].generation):
            r.append("l1:live_not_confirmed")
        if inst.book is None:
            r.append("book:none")
        elif inst.book.state is not BookState.VALID:
            issues = ",".join(sorted(i.value for i in inst.book.issues))
            r.append(f"book:{inst.book.state.value}" + (f"({issues})" if issues else ""))
        for key in self.alerts:
            r.append(f"alert:{key}")
        return r

    # ================================================================== snapshots
    def state_token(self) -> tuple:
        """Cheap fingerprint of every health/quality-relevant fact exposed in snapshots.

        The pipeline publishes a new snapshot IMMEDIATELY whenever this changes, so a published
        snapshot can never keep advertising a state (e.g. market_data_ok with a pre-reset book)
        that the engine has already left. Book row contents are covered by the publish cadence.
        """
        insts = []
        for inst in self.instruments.values():
            b = inst.book
            insts.append((
                inst.instrument_id, inst.contract_state, inst.market_data_type, inst.mdt_generation,
                (b.state, b.epoch, b.needs_resync, b.issues) if b is not None else None,
                tuple((st.generation, st.status, st.error_active) for st in inst.streams.values()),
                self._tape_token(inst),
            ))
        return (self.connection, self.farm_broken, self.not_live, self.conflict.phase, self.conflict.attempts,
                self.resubscribe_all_pending, tuple(self.alerts), tuple(insts))

    def _tape_token(self, inst: InstrumentState) -> tuple:
        cl = inst.classifier
        q = cl.current_quote  # type: ignore[union-attr]
        return (inst.tape.epoch, cl.epoch, self.classification_context(inst)[0],  # type: ignore[union-attr]
                q is not None and q.two_sided)

    def tape_snapshot(self, inst: InstrumentState) -> TapeSnapshot:
        tape, cl = inst.tape, inst.classifier
        last = tape.last()  # type: ignore[union-attr]
        ok, why = self.classification_context(inst)
        q = cl.current_quote  # type: ignore[union-attr]
        return TapeSnapshot(
            size=len(tape), epoch=tape.epoch, classifier_epoch=cl.epoch,  # type: ignore[arg-type,union-attr]
            retained_window=tape.retained_window.frozen(),  # type: ignore[union-attr]
            epoch_cumulative=tape.epoch_cumulative.frozen(),  # type: ignore[union-attr]
            session_cumulative=tape.session_cumulative.frozen(),  # type: ignore[union-attr]
            latest=tape.latest(self._tape_cfg.snapshot_trades),  # type: ignore[union-attr]
            last_aggressor=last.aggressor if last else None, last_method=last.method if last else None,
            last_confidence=last.confidence if last else None, context_ok=ok, context_reason=why,
            has_quote=q is not None and q.two_sided, quote_bid_units=q.bid_units if q else None,
            quote_ask_units=q.ask_units if q else None, tick_direction=cl.tick_direction,  # type: ignore[union-attr]
            evicted_by_count=tape.evicted_by_count, evicted_by_age=tape.evicted_by_age)  # type: ignore[union-attr]

    def snapshot(self) -> MarketSnapshot:
        insts = []
        for inst in self.instruments.values():
            reasons = self.market_data_reasons(inst)
            streams = tuple(
                StreamSnapshot(s, st.generation, self.effective_status(st), st.last_event_mono_ns, st.events,
                               st.requests, st.last_error_code)
                for s, st in inst.streams.items())
            insts.append(InstrumentSnapshot(
                instrument_id=inst.instrument_id, local_symbol=inst.local_symbol, con_id=inst.con_id,
                contract_state=inst.contract_state,
                book=inst.book.snapshot() if inst.book is not None else None,
                bbo=inst.bbo, last_trade=inst.last_trade, market_data_type=inst.market_data_type,
                streams=streams, market_data_ok=not reasons, not_ok_reasons=tuple(reasons),
                tape=self.tape_snapshot(inst)))
        return MarketSnapshot(
            seq=self.last_seq, mono_ns=self.last_mono_ns, wall_ns=self.last_wall_ns,
            connection=self.connection, farm_broken=self.farm_broken,
            conflict_phase=self.conflict.phase.value, conflict_attempts=self.conflict.attempts,
            not_live=self.not_live, alerts=tuple(sorted(self.alerts)), instruments=tuple(insts))
