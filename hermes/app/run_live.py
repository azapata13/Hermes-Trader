"""Hermès Phase C live entry point (market intelligence only — READ-ONLY, no orders).

    python -m hermes.app.run_live [--config config/hermes.toml] [--duration 60] [--no-record] [--quiet]

Signals: SIGINT/SIGTERM = clean shutdown (cancel subscriptions, drain recorder, footer);
         SIGUSR1 = operator retry (resets depth-resync and 10197 retry budgets).

Exit status: 0 = healthy run, 1 = unhealthy (see printed summary), 2 = configuration error.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import ibapi

from hermes.config import ConfigError, HermesConfig, load_config, DEFAULT_CONFIG_PATH
from hermes.core.logging_setup import setup_logging
from hermes.core.telemetry import Reporter, Telemetry
from hermes.ibkr import raw_events as R
from hermes.ibkr.adapter import RawPipeline
from hermes.ibkr.gateway import RequestGateway
from hermes.ibkr.normalizer import NORMALIZER_VERSION, Normalizer
from hermes.ibkr.session import Heartbeat, IbkrSession, Supervisor, spec_from_config
from hermes.market.engine import MarketEngine
from hermes.market.snapshot import MarketSnapshot, SnapshotPublisher
from hermes.storage.reader import verify_session
from hermes.storage.recorder import Recorder

HERMES_VERSION = "0.4.0-c3"
log = logging.getLogger("hermes.app")
_REPO = Path(__file__).resolve().parents[2]


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=_REPO, capture_output=True, text=True,
                             timeout=2, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=_REPO, capture_output=True, text=True,
                               timeout=2, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        return out.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "") if out.returncode == 0 else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def build_meta(cfg: HermesConfig) -> dict[str, Any]:
    return {
        "hermes_version": HERMES_VERSION, "git_commit": git_commit(), "ibapi_version": ibapi.__version__,
        "python": platform.python_version(), "platform": platform.platform(),
        "normalizer_version": NORMALIZER_VERSION, "contract_spec": dict(spec_from_config(cfg).to_params()),
        "config": dataclasses.asdict(cfg),
    }


def _fmt_price(units: int | None, grid: Any) -> str:
    if units is None:
        return "-"
    return f"{grid.to_price(units):.2f}" if grid is not None else str(units)


class LiveRuntime:
    """Wires the live C3 system together. Also used by the fake-TWS end-to-end tests."""

    def __init__(self, cfg: HermesConfig, record: bool = True) -> None:
        self.cfg = cfg
        self.started_mono = time.perf_counter_ns()
        self.telemetry = Telemetry()
        self.publisher = SnapshotPublisher()
        self.normalizer = Normalizer()
        self.engine = MarketEngine(cfg.book, cfg.session, cfg.subscriptions, tape_cfg=cfg.tape)
        self.recorder = Recorder(cfg.recorder, build_meta(cfg)) if (record and cfg.recorder.enabled) else None
        self.pipeline = RawPipeline(self.normalizer, self.engine, self.telemetry, self.publisher, self.recorder,
                                    tick_interval_ns=cfg.session.clock_tick_interval_ms * 1_000_000,
                                    snapshot_interval_ns=cfg.telemetry.snapshot_interval_ms * 1_000_000)
        self.gateway = RequestGateway(self.pipeline.post_local, cfg.gateway)
        self.session = IbkrSession(cfg, self.pipeline, self.gateway)
        self.pipeline.consumers.append(self.session)
        self.supervisor = Supervisor(cfg, self.pipeline, self.gateway, self.session)
        self.max_msg_queue = 0
        self._violations_reported = 0
        self.heartbeat = Heartbeat(self.gateway, cfg.ibkr.heartbeat_interval_ms, on_beat=self._on_beat)
        self.reporter = Reporter(self.build_report, cfg.telemetry.report_interval_s, console=self.console_line)
        self.verification = None

    # ------------------------------------------------------------------ lifecycle
    def run(self, duration_s: float | None = None, install_signals: bool = True) -> dict[str, Any]:
        if self.recorder is not None:
            self.recorder.start()
            log.info("recording to %s", self.recorder.session_dir)
        if install_signals:
            self.supervisor.install_signal_handlers()
        self.heartbeat.start()
        self.reporter.start()
        try:
            self.supervisor.run(duration_s)
        finally:
            self.heartbeat.stop()
            self.pipeline.pump()
            self.reporter.stop()
            if self.recorder is not None:
                self.recorder.stop()
                self.verification = verify_session(self.recorder.session_dir)
        return self.summary()

    def msg_queue_depth(self) -> int:
        c = self.supervisor.client
        try:
            return c.msg_queue.qsize() if c is not None else 0
        except Exception:  # noqa: BLE001
            return -1

    def _on_beat(self) -> None:
        q = self.msg_queue_depth()
        if q > self.max_msg_queue:
            self.max_msg_queue = q
        c = self.supervisor.client
        n = len(c.readonly_violations) if c is not None else 0
        if n > self._violations_reported:
            self._violations_reported = n
            self.pipeline.post_local(R.RawControl, kind="readonly_violation", detail=c.readonly_violations[-1])

    # ------------------------------------------------------------------ reporting
    def build_report(self) -> dict[str, Any]:
        now = time.perf_counter_ns()
        snap: MarketSnapshot | None = self.publisher.latest()
        rep: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                               "uptime_s": round((now - self.started_mono) / 1e9, 1)}
        if snap is not None:
            rep.update({"seq": snap.seq, "connection": snap.connection.value, "farm_broken": snap.farm_broken,
                        "conflict": {"phase": snap.conflict_phase, "attempts": snap.conflict_attempts},
                        "not_live": snap.not_live, "alerts": list(snap.alerts)})
            insts = {}
            engine_insts = dict(self.engine.instruments)
            for i in snap.instruments:
                inst_state = engine_insts.get(i.instrument_id)
                grid = inst_state.grid if inst_state is not None else None
                b = i.book
                book = None
                if b is not None:
                    ob = inst_state.book if inst_state is not None else None
                    c = ob.counters if ob is not None else None
                    book = {"state": b.state.value, "issues": sorted(x.value for x in b.issues), "epoch": b.epoch,
                            "bid": _fmt_price(b.bids[0][0], grid) if b.bids else None,
                            "bid_size": b.bids[0][1] if b.bids else None,
                            "ask": _fmt_price(b.asks[0][0], grid) if b.asks else None,
                            "ask_size": b.asks[0][1] if b.asks else None,
                            "rows": [len(b.bids), len(b.asks)], "needs_resync": b.needs_resync,
                            "stale_reason": b.stale_reason.value if b.stale_reason else None}
                    if c is not None:
                        # dict(...) copies are atomic under the GIL (the dispatch thread may be writing)
                        book.update({"ops": c.ops,
                                     "violations": {k.value: v for k, v in dict(c.violations).items()},
                                     "resets": {k.value: v for k, v in dict(c.resets).items()},
                                     "invalidations": {k.value: v for k, v in dict(c.invalidations).items()},
                                     "transitions": c.transitions})
                streams = {}
                for st in i.streams:
                    age = None if st.last_event_mono_ns is None else round((now - st.last_event_mono_ns) / 1e6, 1)
                    streams[st.stream.value] = {"status": st.status.value, "generation": st.generation,
                                                "age_ms": age, "events": st.events, "requests": st.requests,
                                                "last_error": st.last_error_code}
                insts[i.local_symbol or str(i.instrument_id)] = {
                    "con_id": i.con_id, "contract": i.contract_state, "market_data_ok": i.market_data_ok,
                    "not_ok_reasons": list(i.not_ok_reasons), "market_data_type": i.market_data_type,
                    "book": book, "streams": streams,
                    "last_trade": _fmt_price(i.last_trade.price_units, grid) if i.last_trade else None,
                    "tape": None if i.tape is None else {
                        "size": i.tape.size, "epoch": i.tape.epoch, "context_ok": i.tape.context_ok,
                        "context": i.tape.context_reason,
                        # retained_window totals (bounded tape); epoch/session cumulative reported separately
                        "buy_vol": i.tape.retained_window.buy_volume, "sell_vol": i.tape.retained_window.sell_volume,
                        "unknown_vol": i.tape.retained_window.unknown_volume,
                        "known_delta": i.tape.retained_window.known_delta,
                        "by_method": dict(i.tape.retained_window.by_method),
                        "epoch_cumulative": dataclasses.asdict(i.tape.epoch_cumulative),
                        "session_cumulative": dataclasses.asdict(i.tape.session_cumulative),
                        "last": i.tape.last_aggressor.value if i.tape.last_aggressor else None}}
            rep["instruments"] = insts
        rep["latency_us"] = self.telemetry.swap_latencies()
        if self.recorder is not None:
            st = self.recorder.stats
            rep["recorder"] = {"backlog": self.recorder.backlog, "high_watermark": st.high_watermark,
                               "submitted": st.submitted, "written": st.written,
                               "dropped_overflow": st.dropped_overflow, "lost_write_error": st.lost_write_error,
                               "gaps_queued": st.gaps_queued, "gaps_written": st.gaps_written,
                               "write_errors": st.write_errors, "replay_complete": st.replay_complete,
                               "first_gap_seq": st.first_gap_seq, "file": st.current_file}
        else:
            rep["recorder"] = None
        c = self.supervisor.client
        rep["ibapi_msg_queue"] = {"now": self.msg_queue_depth(), "max": self.max_msg_queue}
        rep["readonly_violations"] = len(c.readonly_violations) if c is not None else 0
        ec = self.engine.counters
        rep["pipeline"] = {"callbacks": self.pipeline.callbacks, "internal_errors": self.pipeline.internal_errors,
                           "seq": self.pipeline.seq, "snapshots": self.publisher.published}
        rep["engine"] = {"events": ec.events, "stale_generation_rejected": ec.stale_generation_rejected,
                         "inactive_callbacks": ec.inactive_callbacks, "unknown_req_id": ec.unknown_req_id,
                         "anomalies": dict(ec.anomalies), "errors_by_code": dict(ec.errors_by_code),  # atomic copies
                         "bbo_frozen_suspect": ec.bbo_frozen_suspect}
        rep["gateway"] = {"sent": self.gateway.sent, "rate_limited": self.gateway.rate_limited,
                          "failed": self.gateway.failed}
        rep["session_phase"] = self.session.phase.value
        return rep

    @staticmethod
    def console_line(rep: dict[str, Any]) -> str:
        parts = [rep.get("connection", "?").upper()]
        for sym, i in (rep.get("instruments") or {}).items():
            b = i.get("book") or {}
            top = (f"{b.get('bid')}x{b.get('bid_size')} / {b.get('ask')}x{b.get('ask_size')}"
                   if b.get("bid") is not None and b.get("ask") is not None else "no book")
            parts.append(f"{sym} book={str(b.get('state', '-')).upper()} {top}")
            parts.append(" ".join(f"{k}={v['status']}({v['age_ms']}ms)" for k, v in i["streams"].items()))
            parts.append("OK" if i["market_data_ok"] else "NOT-OK: " + ",".join(i["not_ok_reasons"][:4]))
        lat = rep.get("latency_us", {})
        cb, core = lat.get("callback_total"), lat.get("core_total")
        if cb:
            parts.append(f"cb p50/p99={cb['p50']}/{cb['p99']}us")
        if core:
            parts.append(f"core p99={core['p99']}us")
        trade = lat.get("trade_classify_tape")
        for i in (rep.get("instruments") or {}).values():
            t = i.get("tape")
            if t:
                parts.append(f"tape n={t['size']} B/S/U={t['buy_vol']}/{t['sell_vol']}/{t['unknown_vol']}"
                             + (f" cls p99={trade['p99']}us" if trade else ""))
        r = rep.get("recorder")
        if r:
            parts.append(f"rec backlog={r['backlog']} drops={r['dropped_overflow']} gaps={r['gaps_written']}")
        if rep.get("alerts"):
            parts.append("ALERTS=" + ",".join(rep["alerts"]))
        return " | ".join(parts)

    def summary(self) -> dict[str, Any]:
        snap = self.supervisor.final_snapshot or self.publisher.latest()
        inst = snap.instruments[0] if snap is not None and snap.instruments else None
        ver = self.verification
        c = self.supervisor.client
        violations = len(c.readonly_violations) if c is not None else 0
        first_ok_s = None
        if self.pipeline.first_ok_mono_ns is not None:
            first_ok_s = round((self.pipeline.first_ok_mono_ns - self.started_mono) / 1e9, 2)
        problems = []
        if not self.pipeline.ever_market_data_ok:
            problems.append("market data never became OK")
        if inst is not None and not inst.market_data_ok:
            problems.append("not OK before shutdown: " + ", ".join(inst.not_ok_reasons))
        if snap is not None and snap.alerts:
            problems.append("alerts: " + ", ".join(snap.alerts))
        if violations:
            problems.append(f"{violations} read-only violation(s)")
        if self.pipeline.internal_errors:
            problems.append(f"{self.pipeline.internal_errors} internal error(s)")
        if ver is not None and not ver.replay_complete:
            problems.append("recording NOT replay-complete: " + "; ".join(ver.problems))
        return {
            "healthy": not problems, "problems": problems, "time_to_market_data_ok_s": first_ok_s,
            "contract": inst.local_symbol if inst else None, "con_id": inst.con_id if inst else None,
            "book_state_before_shutdown": inst.book.state.value if inst and inst.book else None,
            "raw_events": self.pipeline.seq, "callbacks": self.pipeline.callbacks,
            "recording": str(self.recorder.session_dir) if self.recorder else None,
            "replay_complete": ver.replay_complete if ver else None,
            "connect_attempts": self.supervisor.connect_attempts,
        }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Hermès Phase C live market engine (READ-ONLY)")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--duration", type=float, default=None, help="stop after N seconds (smoke test)")
    ap.add_argument("--no-record", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="no console telemetry lines")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    handle = setup_logging(cfg.telemetry.log_directory, console=cfg.telemetry.console and not args.quiet)
    log.info("Hermès %s Phase C (READ-ONLY, no order path) starting; logs in %s", HERMES_VERSION, handle.log_dir)
    try:
        rt = LiveRuntime(cfg, record=not args.no_record)
        summary = rt.run(args.duration)
    finally:
        handle.stop()
    print("\n=========== HERMÈS C3 RUN SUMMARY ===========")
    for k, v in summary.items():
        if k != "problems":
            print(f"{k:28s} {v}")
    print(f"{'RESULT':28s} {'HEALTHY' if summary['healthy'] else 'UNHEALTHY'}")
    for p in summary["problems"]:
        print(f"  ! {p}")
    return 0 if summary["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
