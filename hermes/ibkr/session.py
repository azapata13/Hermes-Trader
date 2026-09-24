"""Live IBKR session: lifecycle, subscriptions, depth resync, bounded 10197 recovery, reconnect.

``IbkrSession`` is an in-thread consumer of the pipeline (runs on the dispatch thread after each
raw event). It reads engine state and issues requests through the RequestGateway. Its
decisions are live-only; everything that influences engine state is recorded (requests via the
gateway, decisions via ``RawControl`` events), so replay does not need the session.

``Heartbeat`` (own thread) sends ``reqCurrentTime`` every ``heartbeat_interval_ms``: TWS
liveness + guaranteed callbacks (and therefore timer ticks) in quiet markets.

``Supervisor`` (main thread) connects with a fresh ``ReadOnlyClient`` per attempt, runs the
official ``EClient.run()`` on the "ibkr-dispatch" thread, reconnects with bounded exponential
backoff, and shuts down cleanly (cancel subscriptions, drain recorder).

Retry budgets are bounded: depth resync (``resync_max_per_window`` per ``resync_window_s``)
and 10197 recovery (``conflict_max_attempts``). Exhausted budgets raise critical alerts and
stop automatic retries until a connection/session change or an operator retry (SIGUSR1).
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections import deque
from enum import Enum
from typing import Callable

from ibapi.contract import Contract

from hermes.config import HermesConfig
from hermes.ibkr import raw_events as R
from hermes.ibkr.adapter import IbkrAdapter, RawPipeline
from hermes.ibkr.contracts import ContractSpec
from hermes.ibkr.gateway import GatewayError, RequestGateway
from hermes.ibkr.readonly import ReadOnlyClient
from hermes.market import events as M
from hermes.market.events import ConnectionState, Stream

log = logging.getLogger("hermes.session")
_S = 1_000_000_000


class Phase(Enum):
    WAIT_CONNECT = "wait_connect"
    RESOLVING = "resolving_contract"
    MARKET_RULE = "market_rule"
    STREAMING = "streaming"
    FAILED = "failed"


def spec_from_config(cfg: HermesConfig) -> ContractSpec:
    i = cfg.instrument
    return ContractSpec(i.symbol, i.sec_type, i.exchange, i.currency, i.trading_class,
                        i.last_trade_date_or_contract_month)


def lookup_contract(spec: ContractSpec) -> Contract:
    c = Contract()
    c.symbol = spec.symbol
    c.secType = spec.sec_type
    c.exchange = spec.exchange
    c.currency = spec.currency
    c.tradingClass = spec.trading_class
    c.lastTradeDateOrContractMonth = spec.month
    return c


def subscription_contract(con_id: int, exchange: str) -> Contract:
    c = Contract()
    c.conId = con_id
    c.exchange = exchange
    return c


class IbkrSession:
    def __init__(self, cfg: HermesConfig, pipeline: RawPipeline, gateway: RequestGateway) -> None:
        self.cfg = cfg
        self.p = pipeline
        self.engine = pipeline.engine
        self.gw = gateway
        self.iid = cfg.instrument.instrument_id
        self.spec = spec_from_config(cfg)
        self.phase = Phase.WAIT_CONNECT
        self.connected = threading.Event()
        self._phase_started = 0
        self._resolved_once = False
        self._subs: dict[Stream, int | None] = {s: None for s in Stream}
        self._resync_times: deque[int] = deque()
        self._last_resync = -10**18
        self._resync_exhausted = False
        self._conflict_seen_mono: int | None = None
        self._last_conflict_attempt = -10**18
        self._shutting_down = False
        s = cfg.session
        self._contract_timeout = int(s.contract_timeout_s * _S)
        self._rule_timeout = int(s.market_rule_timeout_s * _S)
        self._resync_min = int(s.resync_min_interval_s * _S)
        self._resync_window = int(s.resync_window_s * _S)
        self._conflict_interval = int(s.conflict_retry_interval_s * _S)

    # ------------------------------------------------------------------ consumer hook
    def after_event(self, raw: R.RawEvent, events: tuple[M.MarketEvent, ...], now: int) -> None:
        for ev in events:
            if isinstance(ev, M.ConnectionEvent):
                if ev.state is ConnectionState.CONNECTED:
                    self._on_connected(now)
                elif ev.state is ConnectionState.CLOSED:
                    self.connected.clear()
            elif isinstance(ev, M.ControlEvent) and ev.kind == "operator_retry":
                self._reset_budgets("operator retry")
            elif isinstance(ev, M.ErrorEvent) and ev.error_class is M.ErrorClass.SESSION_CONFLICT:
                if self._conflict_seen_mono is None:
                    self._conflict_seen_mono = now
        if self._shutting_down or self.engine.connection is not ConnectionState.CONNECTED:
            return
        try:
            self._step(now)
        except GatewayError as exc:
            log.warning("request deferred: %s", exc)

    # ------------------------------------------------------------------ lifecycle
    def _on_connected(self, now: int) -> None:
        self.connected.set()
        self._reset_budgets("connection established")
        self._subs = {s: None for s in Stream}           # old reqIds died with the old connection
        try:
            self.gw.req_market_data_type(1)                   # LIVE; must precede reqMktData
            inst = self.engine.instruments.get(self.iid)
            if inst is not None and inst.contract_state == "defined" and inst.con_id is not None:
                self._subscribe_all(now)                      # reconnect: contract already known
                self._set_phase(Phase.STREAMING, now)
            else:
                self.gw.req_contract_details(self.iid, lookup_contract(self.spec), self.spec)
                self._set_phase(Phase.RESOLVING, now)
        except GatewayError as exc:
            log.error("initial requests failed: %s", exc)

    def _set_phase(self, phase: Phase, now: int) -> None:
        if phase is not self.phase:
            log.info("session phase %s -> %s", self.phase.value, phase.value)
        self.phase = phase
        self._phase_started = now

    def _step(self, now: int) -> None:
        eng = self.engine
        inst = eng.instruments.get(self.iid)
        state = inst.contract_state if inst is not None else "pending"
        if self.phase is Phase.RESOLVING:
            if state == "resolved":
                self.gw.req_market_rule(self.iid, self._market_rule_id())
                self._set_phase(Phase.MARKET_RULE, now)
            elif state == "failed":
                self._set_phase(Phase.FAILED, now)
            elif now - self._phase_started > self._contract_timeout:
                self.p.post_local(R.RawControl, kind="contract_timeout", detail=str(self.spec))
                self._set_phase(Phase.FAILED, now)
            return
        if self.phase is Phase.MARKET_RULE:
            if state == "defined":
                self._subscribe_all(now)
                self._set_phase(Phase.STREAMING, now)
            elif state == "failed":
                self._set_phase(Phase.FAILED, now)
            elif now - self._phase_started > self._rule_timeout:
                self.p.post_local(R.RawControl, kind="market_rule_timeout", detail=str(self._market_rule_id()))
                self._set_phase(Phase.FAILED, now)
            return
        if self.phase is not Phase.STREAMING or inst is None:
            return
        # 10197: bounded, paced, proven recovery (the engine decides when it is proven)
        c = eng.conflict
        if c.active:
            if c.can_attempt() and self._conflict_seen_mono is not None:
                since = max(self._last_conflict_attempt, self._conflict_seen_mono)
                if now - since >= self._conflict_interval and self.gw.can_send(self._full_resubscribe_cost()):
                    self._last_conflict_attempt = now
                    log.warning("10197 recovery attempt %d/%d", c.attempts + 1, c.max_attempts)
                    self.p.post_local(R.RawControl, kind="conflict_recovery_attempt", detail=str(c.attempts + 1))
                    self._subscribe_all(now)
            return
        self._conflict_seen_mono = None
        if eng.resubscribe_all_pending:
            if self.gw.can_send(self._full_resubscribe_cost()):
                log.warning("1101 (data lost): resubscribing all streams")
                self._subscribe_all(now)
            return
        missing = [st for st in inst.required if inst.streams[st].generation is None]
        if missing and now - self._last_resync >= self._resync_min and self.gw.can_send(2 * len(missing)):
            # self-healing: a subscription that could not be (re)issued, e.g. rate-limited earlier
            log.warning("re-issuing missing subscription(s): %s", ", ".join(m.value for m in missing))
            for st in missing:
                self._subscribe(st)
            self._last_resync = now
            return
        book = inst.book
        if (book is not None and book.needs_resync and not eng.farm_broken and not eng.not_live
                and not self._resync_exhausted and self.gw.can_send(2)):
            self._resync_depth(now)

    def _full_resubscribe_cost(self) -> int:
        inst = self.engine.instruments.get(self.iid)
        return 2 * len(inst.required) if inst is not None else 8

    def _market_rule_id(self) -> int:
        rc = self.p.normalizer.resolved_contract(self.iid)
        if rc is None:
            raise GatewayError("contract not resolved")
        return rc.market_rule_id

    # ------------------------------------------------------------------ subscriptions
    def _contract(self) -> Contract:
        inst = self.engine.instruments[self.iid]
        return subscription_contract(int(inst.con_id), self.spec.exchange)  # type: ignore[arg-type]

    def _cancel(self, stream: Stream) -> None:
        rid = self._subs.get(stream)
        if rid is None:
            return
        self._subs[stream] = None
        st = self.engine.instruments[self.iid].streams[stream]
        if st.generation != rid:
            return
        smart = self.cfg.subscriptions.depth_smart
        if stream is Stream.DEPTH:
            self.gw.cancel_mkt_depth(self.iid, rid, smart)
        elif stream in (Stream.BBO, Stream.TRADES):
            self.gw.cancel_tick_by_tick(self.iid, rid)
        elif stream is Stream.L1:
            self.gw.cancel_mkt_data(self.iid, rid)

    def _subscribe(self, stream: Stream) -> None:
        self._cancel(stream)
        c = self._contract()
        sub = self.cfg.subscriptions
        if stream is Stream.DEPTH:
            rid = self.gw.req_mkt_depth(self.iid, c, self.cfg.book.depth_rows, sub.depth_smart)
        elif stream is Stream.BBO:
            rid = self.gw.req_tick_by_tick(self.iid, c, "BidAsk")
        elif stream is Stream.TRADES:
            rid = self.gw.req_tick_by_tick(self.iid, c, "AllLast")
        else:
            rid = self.gw.req_mkt_data(self.iid, c)
        self._subs[stream] = rid

    def _subscribe_all(self, now: int) -> None:
        for s in self.engine.instruments[self.iid].required if self.iid in self.engine.instruments else ():
            self._subscribe(s)
        self._last_resync = now

    def _resync_depth(self, now: int) -> None:
        if now - self._last_resync < self._resync_min:
            return
        while self._resync_times and now - self._resync_times[0] > self._resync_window:
            self._resync_times.popleft()
        if len(self._resync_times) >= self.cfg.session.resync_max_per_window:
            self._resync_exhausted = True
            log.critical("depth resync budget exhausted; automatic depth resubscription stopped")
            self.p.post_local(R.RawControl, kind="depth_resync_exhausted",
                              detail=f"{len(self._resync_times)} resyncs within {self.cfg.session.resync_window_s}s")
            return
        self._resync_times.append(now)
        self._last_resync = now
        log.warning("depth resync (%d in window)", len(self._resync_times))
        self._subscribe(Stream.DEPTH)

    def _reset_budgets(self, why: str) -> None:
        if self._resync_exhausted:
            self.p.post_local(R.RawControl, kind="depth_resync_budget_reset", detail=why)
        self._resync_exhausted = False
        self._resync_times.clear()
        self._last_conflict_attempt = -10**18

    def shutdown_requests(self) -> None:
        """Cancel active subscriptions (best effort) before disconnecting."""
        self._shutting_down = True
        if self.iid not in self.engine.instruments:
            return
        for s in (Stream.DEPTH, Stream.BBO, Stream.TRADES, Stream.L1):
            try:
                self._cancel(s)
            except GatewayError:
                pass


class Heartbeat:
    def __init__(self, gateway: RequestGateway, interval_ms: int, on_beat: Callable[[], None] | None = None) -> None:
        self._gw = gateway
        self._interval = interval_ms / 1000.0
        self._on_beat = on_beat
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            if self._on_beat is not None:
                try:
                    self._on_beat()
                except Exception:  # noqa: BLE001
                    log.exception("heartbeat hook failed")
            if self._gw.connected:
                try:
                    self._gw.req_current_time()
                except GatewayError:
                    pass


class Supervisor:
    """Main-thread connection supervisor (reconnect with bounded exponential backoff)."""

    def __init__(self, cfg: HermesConfig, pipeline: RawPipeline, gateway: RequestGateway,
                 session: IbkrSession) -> None:
        self.cfg = cfg
        self.p = pipeline
        self.gw = gateway
        self.session = session
        self.adapter = IbkrAdapter(pipeline)
        self.stop_event = threading.Event()
        self.client: ReadOnlyClient | None = None
        self.dispatch: threading.Thread | None = None
        self.connect_attempts = 0
        self.final_snapshot = None

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, lambda *_: self.request_stop("SIGINT"))
        signal.signal(signal.SIGTERM, lambda *_: self.request_stop("SIGTERM"))
        if hasattr(signal, "SIGUSR1"):
            signal.signal(signal.SIGUSR1, lambda *_: self.operator_retry())

    def request_stop(self, why: str) -> None:
        log.warning("stop requested (%s)", why)
        self.stop_event.set()

    def operator_retry(self) -> None:
        log.warning("operator retry requested (SIGUSR1): retry budgets reset")
        self.p.post_local(R.RawControl, kind="operator_retry", detail="SIGUSR1")

    def run(self, duration_s: float | None = None) -> None:
        s = self.cfg.session
        ib = self.cfg.ibkr
        deadline = time.monotonic() + duration_s if duration_s else None
        backoff = s.reconnect_initial_backoff_s
        while not self.stop_event.is_set() and not self._expired(deadline):
            self.connect_attempts += 1
            self.p.post_local(R.RawControl, kind="connect_attempt", detail=f"{ib.host}:{ib.port} id={ib.client_id}")
            self.session.connected.clear()
            client = ReadOnlyClient(self.adapter)
            self.client = client
            self.gw.bind(client)
            log.info("connecting to TWS %s:%d clientId=%d (attempt %d)", ib.host, ib.port, ib.client_id,
                     self.connect_attempts)
            client.connect(ib.host, ib.port, ib.client_id)
            if client.isConnected():
                self.dispatch = threading.Thread(target=client.run, name="ibkr-dispatch", daemon=True)
                self.dispatch.start()
                if self.session.connected.wait(ib.connect_timeout_s):
                    backoff = s.reconnect_initial_backoff_s
                    while (self.dispatch.is_alive() and not self.stop_event.is_set()
                           and not self._expired(deadline)):
                        self.stop_event.wait(0.2)
                else:
                    log.error("no nextValidId within %.1fs", ib.connect_timeout_s)
                if self.dispatch.is_alive():
                    self._graceful_disconnect(client)
                else:
                    log.error("TWS connection lost")
            else:
                self.p.post_local(R.RawControl, kind="connect_failed", detail="socket connect failed")
                self.p.pump()
            self.gw.unbind()
            if self.stop_event.is_set() or self._expired(deadline):
                break
            log.warning("reconnecting in %.1fs", backoff)
            self._sleep(backoff, deadline)
            backoff = min(backoff * 2, s.reconnect_max_backoff_s)
        self.p.pump()

    def _graceful_disconnect(self, client: ReadOnlyClient) -> None:
        self.final_snapshot = self.p.publisher.latest()     # state before we cancel anything
        try:
            self.session.shutdown_requests()
        except Exception:  # noqa: BLE001
            log.exception("shutdown requests failed")
        time.sleep(self.cfg.session.shutdown_grace_s)
        client.disconnect()
        if self.dispatch is not None:
            self.dispatch.join(5)

    def _expired(self, deadline: float | None) -> bool:
        return deadline is not None and time.monotonic() >= deadline

    def _sleep(self, seconds: float, deadline: float | None) -> None:
        if deadline is not None:
            seconds = min(seconds, max(0.0, deadline - time.monotonic()))
        self.stop_event.wait(seconds)
