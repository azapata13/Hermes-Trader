"""RequestGateway: the ONLY path from Hermès code to TWS requests.

* Accepts only a ``ReadOnlyClient`` (TypeError otherwise).
* Explicit, allowlisted methods — there is no generic "call any EClient method".
* Serializes requests behind one lock (ibapi's EClient is not relied upon to be thread-safe).
* Allocates reqIds: monotonically increasing, never reused within a process run, so every
  (re)subscription is a new generation (C3 amendment A).
* Records every request as ``RawRequestIssued`` with the TRUE send timestamps captured here,
  immediately before the send (amendment D). The event is queued BEFORE the send so it is
  always sequenced ahead of any response callback.
* A conservative global token bucket guards against request floods (amendment F: a safety
  guard, not a model of IBKR pacing; endpoint-specific limits can be added via
  ``endpoint_limits``).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from hermes.config import GatewayConfig
from hermes.ibkr.contracts import ContractSpec
from hermes.ibkr.raw_events import RawRequestFailed, RawRequestIssued
from hermes.ibkr.readonly import ReadOnlyClient, ReadOnlyViolation

log = logging.getLogger("hermes.gateway")

PostLocal = Callable[..., None]


class GatewayError(RuntimeError):
    pass


class NotConnectedError(GatewayError):
    pass


class RateLimitedError(GatewayError):
    pass


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: int, now_ns: Callable[[], int]) -> None:
        self.rate = rate_per_s
        self.burst = float(burst)
        self._tokens = float(burst)
        self._now = now_ns
        self._last = now_ns()

    def _refill(self) -> None:
        now = self._now()
        self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate / 1e9)
        self._last = now

    def available(self) -> float:
        self._refill()
        return self._tokens

    def take(self) -> bool:
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class RequestGateway:
    def __init__(self, post_local: PostLocal, cfg: GatewayConfig, mono_ns: Callable[[], int] = time.perf_counter_ns,
                 wall_ns: Callable[[], int] = time.time_ns,
                 endpoint_limits: dict[str, tuple[float, int]] | None = None) -> None:
        self._post = post_local
        self._mono = mono_ns
        self._wall = wall_ns
        self._lock = threading.Lock()
        self._client: ReadOnlyClient | None = None
        self._next_id = cfg.first_req_id
        self._bucket = TokenBucket(cfg.max_requests_per_second, cfg.burst, mono_ns)
        self._endpoint = {k: TokenBucket(r, b, mono_ns) for k, (r, b) in (endpoint_limits or {}).items()}
        self.sent = 0
        self.rate_limited = 0
        self.failed = 0

    # ------------------------------------------------------------------ binding
    def bind(self, client: ReadOnlyClient) -> None:
        if not isinstance(client, ReadOnlyClient):
            raise TypeError("RequestGateway only accepts a ReadOnlyClient")
        with self._lock:
            self._client = client

    def unbind(self) -> None:
        with self._lock:
            self._client = None

    def can_send(self, n: int) -> bool:
        """True if ``n`` requests fit in the global safety budget right now (multi-request operations
        such as a full resubscription check this first so they are never half-sent)."""
        with self._lock:
            return self._bucket.available() >= n

    @property
    def connected(self) -> bool:
        c = self._client
        return c is not None and c.isConnected()

    # ------------------------------------------------------------------ core
    def _alloc(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    def _send(self, method: str, req_id: int | None, instrument_id: int, params: dict[str, Any],
              call: Callable[[ReadOnlyClient], None]) -> int | None:
        with self._lock:
            client = self._client
            if client is None or not client.isConnected():
                raise NotConnectedError(f"{method}: not connected")
            ep = self._endpoint.get(method)
            if not self._bucket.take() or (ep is not None and not ep.take()):
                self.rate_limited += 1
                raise RateLimitedError(f"{method}: request rate limit reached")
            sent_m, sent_w = self._mono(), self._wall()
            p = tuple((k, str(v)) for k, v in params.items())
            self._post(RawRequestIssued, method=method, req_id=req_id, instrument_id=instrument_id,
                       sent_mono_ns=sent_m, sent_wall_ns=sent_w, params=p)
            try:
                call(client)
            except ReadOnlyViolation:
                log.critical("READ-ONLY VIOLATION via gateway method %s", method)
                raise
            except Exception as exc:  # noqa: BLE001
                self.failed += 1
                self._post(RawRequestFailed, method=method, req_id=req_id, instrument_id=instrument_id,
                           error=repr(exc))
                log.error("request %s(reqId=%s) failed locally: %s", method, req_id, exc)
                return None
            self.sent += 1
            return req_id

    # ------------------------------------------------------------------ allowlisted requests
    def req_current_time(self) -> None:
        self._send("reqCurrentTime", None, 0, {}, lambda c: c.reqCurrentTime())

    def req_market_data_type(self, market_data_type: int = 1) -> None:
        self._send("reqMarketDataType", None, 0, {"market_data_type": market_data_type},
                   lambda c: c.reqMarketDataType(market_data_type))

    def req_contract_details(self, instrument_id: int, contract: Any, spec: ContractSpec) -> int | None:
        with self._lock:
            rid = self._alloc()
        return self._send("reqContractDetails", rid, instrument_id, dict(spec.to_params()),
                          lambda c: c.reqContractDetails(rid, contract))

    def req_market_rule(self, instrument_id: int, market_rule_id: int) -> None:
        self._send("reqMarketRule", None, instrument_id, {"rule_id": market_rule_id},
                   lambda c: c.reqMarketRule(market_rule_id))

    def req_mkt_depth(self, instrument_id: int, contract: Any, num_rows: int, smart: bool) -> int | None:
        with self._lock:
            rid = self._alloc()
        return self._send("reqMktDepth", rid, instrument_id,
                          {"con_id": getattr(contract, "conId", ""), "num_rows": num_rows, "smart": smart},
                          lambda c: c.reqMktDepth(rid, contract, num_rows, smart, []))

    def cancel_mkt_depth(self, instrument_id: int, req_id: int, smart: bool) -> None:
        self._send("cancelMktDepth", req_id, instrument_id, {"smart": smart},
                   lambda c: c.cancelMktDepth(req_id, smart))

    def req_tick_by_tick(self, instrument_id: int, contract: Any, tick_type: str) -> int | None:
        if tick_type not in ("AllLast", "BidAsk"):
            raise ValueError(f"unsupported tick-by-tick type {tick_type!r}")
        with self._lock:
            rid = self._alloc()
        return self._send("reqTickByTickData", rid, instrument_id,
                          {"con_id": getattr(contract, "conId", ""), "tick_type": tick_type},
                          lambda c: c.reqTickByTickData(rid, contract, tick_type, 0, False))

    def cancel_tick_by_tick(self, instrument_id: int, req_id: int) -> None:
        self._send("cancelTickByTickData", req_id, instrument_id, {}, lambda c: c.cancelTickByTickData(req_id))

    def req_mkt_data(self, instrument_id: int, contract: Any) -> int | None:
        with self._lock:
            rid = self._alloc()
        return self._send("reqMktData", rid, instrument_id, {"con_id": getattr(contract, "conId", "")},
                          lambda c: c.reqMktData(rid, contract, "", False, False, []))

    def cancel_mkt_data(self, instrument_id: int, req_id: int) -> None:
        self._send("cancelMktData", req_id, instrument_id, {}, lambda c: c.cancelMktData(req_id))
