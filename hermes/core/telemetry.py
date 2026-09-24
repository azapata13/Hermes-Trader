"""Telemetry registry (dispatch-thread writes, reporter-thread reads) and periodic reporter.

Writes on the dispatch thread are plain integer/dict operations (no locks, no I/O). The
reporter thread reads them periodically; races only affect which window an observation lands
in. Reports go to the ``hermes.telemetry`` logger (JSON lines via the non-blocking logging
queue) and an optional one-line console summary.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

from hermes.core.latency import LatencyHistogram

log_json = logging.getLogger("hermes.telemetry")
log_console = logging.getLogger("hermes.console")


class Telemetry:
    def __init__(self) -> None:
        self.hist: dict[str, LatencyHistogram] = {}
        self.counters: dict[str, int] = {}
        self.gauges: dict[str, Callable[[], Any]] = {}

    def observe(self, name: str, ns: int) -> None:
        h = self.hist.get(name)
        if h is None:
            h = self.hist[name] = LatencyHistogram()
        h.record(ns)

    def incr(self, name: str, n: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + n

    def gauge(self, name: str, fn: Callable[[], Any]) -> None:
        self.gauges[name] = fn

    def swap_latencies(self) -> dict[str, dict[str, float]]:
        return {name: h.swap().as_us() for name, h in list(self.hist.items()) if h.count}

    def read_gauges(self) -> dict[str, Any]:
        out = {}
        for name, fn in list(self.gauges.items()):
            try:
                out[name] = fn()
            except Exception as exc:  # noqa: BLE001 - telemetry must never break anything
                out[name] = f"error: {exc}"
        return out


class Reporter:
    """Background thread: every ``interval_s`` build a report dict and log it."""

    def __init__(self, build: Callable[[], dict[str, Any]], interval_s: float,
                 console: Callable[[dict[str, Any]], str] | None = None) -> None:
        self._build = build
        self._interval = interval_s
        self._console = console
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_report: dict[str, Any] | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="telemetry", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
        self.report_now()

    def report_now(self) -> dict[str, Any] | None:
        try:
            rep = self._build()
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("hermes").exception("telemetry build failed: %s", exc)
            return None
        self.last_report = rep
        log_json.info(json.dumps(rep, default=str, separators=(",", ":")))
        if self._console is not None:
            try:
                log_console.info(self._console(rep))
            except Exception:  # noqa: BLE001
                pass
        return rep

    def _run(self) -> None:
        next_t = time.monotonic() + self._interval
        while not self._stop.wait(max(0.0, next_t - time.monotonic())):
            self.report_now()
            next_t += self._interval
