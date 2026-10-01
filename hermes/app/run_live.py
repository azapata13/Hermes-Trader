"""Hermès Phase C live entry point (market intelligence + HUMAN_APPROVAL decision proposals —
READ-ONLY, no orders, no execution path).

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

import hermes
from hermes.config import DEFAULT_CONFIG_PATH, ConfigError, HermesConfig, load_config
from hermes.core.logging_setup import setup_logging
from hermes.core.telemetry import Reporter, Telemetry
from hermes.decision.approval import current_approval_payload, render_approval_text
from hermes.decision.runtime import DecisionRuntime, JournalKind, JournalRecord
from hermes.ibkr import raw_events as R
from hermes.ibkr.adapter import RawPipeline
from hermes.ibkr.gateway import RequestGateway
from hermes.ibkr.normalizer import NORMALIZER_VERSION, Normalizer
from hermes.ibkr.session import Heartbeat, IbkrSession, Supervisor, spec_from_config
from hermes.market.engine import MarketEngine
from hermes.market.snapshot import MarketSnapshot, SnapshotPublisher
from hermes.replay import fingerprint as fp
from hermes.replay.checkpoints import SIDECAR_NAME, Checkpointer
from hermes.replay.decisions import DECISIONS_SIDECAR, decision_meta, save_decisions
from hermes.replay.warm_start import WarmStartResult, warm_start_engine
from hermes.slack.approvers import ApproverConfigError, ApproverPolicy
from hermes.slack.bridge import SlackApprovalBridge
from hermes.slack.journal import ApprovalJournal
from hermes.slack.socket_mode import (
    SlackConfigError,
    SlackSettings,
    SlackSocketModeTransport,
)
from hermes.storage.reader import verify_session
from hermes.storage.recorder import Recorder

HERMES_VERSION = hermes.__version__
log = logging.getLogger("hermes.app")
dlog = logging.getLogger("hermes.decision")
_REPO = Path(__file__).resolve().parents[2]
_SLACK_VIEW_KINDS = frozenset({
    JournalKind.APPROVAL_VIEW_CREATED,
    JournalKind.TEMPORARY_HOLD_ENTERED,
    JournalKind.TEMPORARY_HOLD_CLEARED,
    JournalKind.CANDIDATE_BLOCKED,
    JournalKind.CANDIDATE_STALE,
    JournalKind.CANDIDATE_INVALIDATED,
    JournalKind.CANDIDATE_EXPIRED,
})


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=_REPO, capture_output=True, text=True,
                             timeout=2, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, check=False)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=_REPO, capture_output=True, text=True,
                               timeout=2, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, check=False)
        return out.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "") if out.returncode == 0 else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def build_meta(cfg: HermesConfig) -> dict[str, Any]:
    return {
        "hermes_version": HERMES_VERSION, "git_commit": git_commit(), "ibapi_version": ibapi.__version__,
        "python": platform.python_version(), "platform": platform.platform(),
        "normalizer_version": NORMALIZER_VERSION, "contract_spec": dict(spec_from_config(cfg).to_params()),
        "config": dataclasses.asdict(cfg), "code_fingerprint": fp.code_fingerprint(),
        "config_fingerprint": fp.config_fingerprint(cfg), "hash_version": fp.HASH_VERSION,
    }


def _fmt_price(units: int | None, grid: Any) -> str:
    if units is None:
        return "-"
    return f"{grid.to_price(units):.2f}" if grid is not None else str(units)


def _bar_brief(b: Any, grid: Any) -> dict[str, Any] | None:
    if b is None:
        return None
    return {"start_s": b.start_s, "o": _fmt_price(b.open, grid), "h": _fmt_price(b.high, grid),
            "l": _fmt_price(b.low, grid), "c": _fmt_price(b.close, grid), "vol": b.volume,
            "bsu": [b.buy_volume, b.sell_volume, b.unknown_volume], "delta": b.known_delta,
            "flags": b.flags.name if b.flags else ""}


def _bars_report(bs: Any, grid: Any) -> dict[str, Any] | None:
    if bs is None:
        return None
    return {"completed": [bs.completed_30s, bs.completed_1m, bs.completed_5m],
            "last_30s": _bar_brief(bs.latest_30s[0] if bs.latest_30s else None, grid),
            "forming_30s": _bar_brief(bs.forming_30s, grid),
            "late": [bs.late_trades, bs.late_volume], "excluded": [bs.excluded_trades, bs.excluded_volume],
            "excluded_by_reason": dict(bs.excluded_by_reason), "empty_bars": bs.empty_bars,
            "gap_bars": bs.gap_bars, "active_flags": bs.active_flags.name if bs.active_flags else ""}


def _session_report(ss: Any, grid: Any) -> dict[str, Any] | None:
    if ss is None:
        return None
    st = ss.session
    return {"calendar_ok": ss.calendar_ok, "calendar_error": ss.calendar_error, "in_session": ss.in_trading_session,
            "in_rth": ss.in_rth, "trading_date": ss.trading_date,
            "vwap": _fmt_price(round(st.vwap_num / st.volume), grid) if st is not None and st.volume else None,
            "high": _fmt_price(st.high, grid) if st else None, "low": _fmt_price(st.low, grid) if st else None,
            "volume": st.volume if st else 0, "observed_from_open": ss.observed_from_open,
            "gap_observed": ss.gap_observed, "previous": ss.previous is not None,
            "outside_session": ss.trades_outside_session, "late_session": ss.late_session_trades}


class LiveRuntime:
    """Wires the live C3 system together. Also used by the fake-TWS end-to-end tests."""

    def __init__(self, cfg: HermesConfig, record: bool = True) -> None:
        self.cfg = cfg
        self.started_mono = time.perf_counter_ns()
        self.telemetry = Telemetry()
        self.publisher = SnapshotPublisher()
        self.normalizer = Normalizer()
        self.engine = MarketEngine(cfg.book, cfg.session, cfg.subscriptions, tape_cfg=cfg.tape, bars_cfg=cfg.bars)
        self.recorder = Recorder(cfg.recorder, build_meta(cfg)) if (record and cfg.recorder.enabled) else None
        self.pipeline = RawPipeline(self.normalizer, self.engine, self.telemetry, self.publisher, self.recorder,
                                    tick_interval_ns=cfg.session.clock_tick_interval_ms * 1_000_000,
                                    snapshot_interval_ns=cfg.telemetry.snapshot_interval_ms * 1_000_000)
        self.gateway = RequestGateway(self.pipeline.post_local, cfg.gateway)
        self.session = IbkrSession(cfg, self.pipeline, self.gateway)
        self.pipeline.consumers.append(self.session)
        # C6: deterministic checkpoints on the dispatch thread (same code as replay); written at shutdown
        self.checkpointer = Checkpointer(self.engine)
        self.pipeline.consumers.append(self.checkpointer)
        self.checkpoint_file: Path | None = None
        # C9f: the SAME decision runtime as replay, as a read-only consumer AFTER the checkpointer
        # (replay runs it after the checkpointer too). Proposals only: HUMAN_APPROVAL, no order path.
        self.slack_bridge: SlackApprovalBridge | None = None
        self.slack_config_error: str | None = None
        self.decisions: DecisionRuntime | None = (
            DecisionRuntime(self.engine, cfg.decision, on_record=self._on_decision) if cfg.decision.enabled else None)
        if self.decisions is not None:
            self.pipeline.consumers.append(self.decisions)
            try:
                slack_settings = SlackSettings.from_env()
                approvers = ApproverPolicy.from_env()
            except (SlackConfigError, ApproverConfigError) as exc:
                self.slack_config_error = str(exc)
                log.error("Slack disabled by configuration error: %s", exc)
            else:
                if slack_settings is not None:
                    log.info("Slack approvers: %s%s", approvers.describe(),
                             "" if approvers.configured else
                             " (ENTER can be recorded as intent but never authorizes execution)")
                    journal_path = os.environ.get("HERMES_SLACK_JOURNAL")
                    if not journal_path:
                        journal_path = str(Path(cfg.telemetry.log_directory).expanduser() / "human_approvals.jsonl")
                    self.slack_bridge = SlackApprovalBridge(
                        self.decisions,
                        self.engine,
                        cfg.decision,
                        SlackSocketModeTransport(slack_settings),
                        ApprovalJournal(journal_path),
                        market_context_provider=self._slack_market_context,
                        status_provider=self._slack_status,
                        approvers=approvers,
                    )
                    # D1: process already-ACKed Slack intents on the same single-writer dispatch thread,
                    # AFTER DecisionRuntime has applied the current market event.
                    self.pipeline.consumers.append(self.slack_bridge)
        self.decision_file: Path | None = None
        self.supervisor = Supervisor(cfg, self.pipeline, self.gateway, self.session)
        self.max_msg_queue = 0
        self._violations_reported = 0
        self.heartbeat = Heartbeat(self.gateway, cfg.ibkr.heartbeat_interval_ms, on_beat=self._on_beat)
        self.reporter = Reporter(self.build_report, cfg.telemetry.report_interval_s, console=self.console_line)
        self.verification = None
        self.warm_start_result: WarmStartResult | None = None

    # ------------------------------------------------------------------ lifecycle
    def run(self, duration_s: float | None = None, install_signals: bool = True) -> dict[str, Any]:
        self._warm_start()
        self.start_recording()
        if self.slack_bridge is not None:
            self.slack_bridge.start()
        if install_signals:
            self.supervisor.install_signal_handlers()
        self.heartbeat.start()
        self.reporter.start()
        try:
            self.supervisor.run(duration_s)
        finally:
            self.heartbeat.stop()
            self.finish()
        return self.summary()

    def _warm_start(self) -> None:
        try:
            self.warm_start_result = warm_start_engine(
                self.engine,
                self.cfg.recorder.directory,
                expected_contract_spec=dict(
                    spec_from_config(self.cfg).to_params()
                ),
            )

            result = self.warm_start_result
            if self.recorder is not None and (result.used or result.applied_events):
                # D2.4: the new recording names the history it started from, so a replay can
                # rebuild the same starting state and verify live equivalence.
                self.recorder.set_meta("warm_start", result.provenance(self.cfg.recorder.directory))

            if result.used:
                log.info(
                    "WARM START | applied | age=%.1fs | raw=%d normalized=%d "
                    "| %s conId=%s | 30s=%d 1m=%d 5m=%d | tape=%d | "
                    "volume=%d | VWAP=%s",
                    result.age_s or 0,
                    result.raw_events,
                    result.normalized_events,
                    result.local_symbol,
                    result.con_id,
                    result.bars_30s,
                    result.bars_1m,
                    result.bars_5m,
                    result.tape_size,
                    result.session_volume,
                    result.session_vwap,
                )
            else:
                log.info(
                    "WARM START | skipped | reason=%s",
                    result.reason,
                )
        except Exception:
            log.exception(
                "WARM START failed; continuing with cold live startup"
            )

    def start_recording(self) -> None:
        if self.recorder is not None:
            self.recorder.start()
            log.info("recording to %s", self.recorder.session_dir)

    def finish(self) -> None:
        """Shutdown bookkeeping once the dispatch thread has ended (single reader from here on)."""
        self.pipeline.pump()
        self.checkpointer.finalize()
        if self.decisions is not None:
            self.decisions.finalize()
        if self.slack_bridge is not None:
            self.slack_bridge.close()
        self.reporter.stop()
        if self.recorder is not None:
            self.recorder.stop()
            self.verification = verify_session(self.recorder.session_dir)
            self._save_checkpoints()
            self._save_decisions()

    def _save_decisions(self) -> None:
        """Persist the decision journal + decision checkpoints (separate from market checkpoints)."""
        if self.decisions is None:
            return
        try:
            self.decision_file = save_decisions(
                self.decisions, Path(self.recorder.session_dir) / DECISIONS_SIDECAR,  # type: ignore[union-attr]
                source="live", session_id=self.recorder.session_id,  # type: ignore[union-attr]
                hermes_version=HERMES_VERSION,
                **decision_meta(self.cfg, fp.code_fingerprint(), fp.config_fingerprint(self.cfg)))
        except OSError as exc:
            log.error("could not write the decision journal: %s", exc)

    def _on_decision(self, r: JournalRecord, view) -> None:
        """Compact decision log line per journal record (decision transitions only, never per tick)."""
        blocks = [f"{c[1]}/{c[2]}" for c in r.reasons if c[0] in ("BLOCK", "HOLD")][:4]
        dlog.info("%s seq=%d %s %s %s%s%s%s", r.kind.value, r.seq, r.setup_id or "-", r.status or "-",
                  r.direction or "", f" proposal={r.proposal_id}" if r.proposal_id else "",
                  f" view={r.approval_view_id}" if r.approval_view_id else "",
                  (" " + ",".join(blocks)) if blocks else "")
        if view is not None and r.kind is JournalKind.APPROVAL_VIEW_CREATED:
            dlog.info("HUMAN_APPROVAL view (inspection only; Hermès sends no order):\n%s", render_approval_text(view))

        # D1: Slack is an asynchronous adapter. Build a fresh immutable approval view only on
        # meaningful candidate transitions (rare), then enqueue it. No Slack/network call happens here.
        bridge = self.slack_bridge
        if bridge is not None and r.kind in _SLACK_VIEW_KINDS:
            slack_view = view
            if slack_view is None and r.setup_id is not None and self.decisions is not None:
                rec = self.decisions.driver.lifecycle.get(r.setup_id)
                if rec is not None and rec.candidate.direction.value != "NONE":
                    try:
                        slack_view = current_approval_payload(
                            self.engine,
                            rec,
                            self.cfg.decision,
                            now_wall_ns=r.wall_ns,
                            instrument_id=rec.candidate.instrument_id,
                        )
                    except Exception:  # Slack must never stop market processing
                        log.exception("could not build Slack approval view")
            bridge.publish_decision(r, slack_view)

    def _save_checkpoints(self) -> None:
        """Persist live checkpoints next to the recording (never on the dispatch thread)."""
        try:
            self.checkpoint_file = self.checkpointer.save(
                Path(self.recorder.session_dir) / SIDECAR_NAME,  # type: ignore[union-attr]
                source="live", session_id=self.recorder.session_id,  # type: ignore[union-attr]
                hermes_version=HERMES_VERSION, code_fingerprint=fp.code_fingerprint(),
                config_fingerprint=fp.config_fingerprint(self.cfg), normalizer_version=NORMALIZER_VERSION)
        except OSError as exc:
            log.error("could not write live checkpoints: %s", exc)

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

    def _slack_status(self) -> str:
        """Fast deterministic Slack status. No OpenAI and no screenshot."""
        snap = self.publisher.latest()

        if snap is None:
            return (
                "🔴 *Hermès STATUS*\n"
                "IBKR: UNKNOWN\n"
                "Context: *UNAVAILABLE*\n"
                "Execution: *DISABLED*"
            )

        now = time.perf_counter_ns()
        age_ms = max(0.0, (now - snap.mono_ns) / 1_000_000)
        uptime_s = max(0.0, (now - self.started_mono) / 1_000_000_000)

        missing: list[str] = []
        instrument_lines: list[str] = []

        connection_ok = snap.connection.value == "connected"

        if not connection_ok:
            missing.append("IBKR")

        if age_ms > 5000:
            missing.append("stale_snapshot")

        if not snap.instruments:
            missing.append("instrument_snapshot")

        overall_ready = connection_ok and age_ms <= 5000 and bool(snap.instruments)

        for inst in snap.instruments:
            b = inst.bars

            completed_30s = b.completed_30s if b is not None else 0
            completed_1m = b.completed_1m if b is not None else 0
            completed_5m = b.completed_5m if b is not None else 0

            bars_ready = (
                completed_30s >= 10
                and completed_1m >= 5
                and completed_5m >= 1
            )

            bbo_ready = inst.bbo is not None

            book_ready = (
                inst.book is not None
                and inst.book.state.value == "valid"
            )

            tape_ready = (
                inst.tape is not None
                and inst.tape.size > 0
            )

            session_ready = False
            if inst.session is not None:
                st = inst.session.session
                session_ready = bool(st is not None and st.volume)

            if not inst.market_data_ok:
                missing.append(f"{inst.local_symbol}:market_data")
            if not bbo_ready:
                missing.append(f"{inst.local_symbol}:BBO")
            if not book_ready:
                missing.append(f"{inst.local_symbol}:DOM")
            if not tape_ready:
                missing.append(f"{inst.local_symbol}:tape")
            if completed_30s < 10:
                missing.append(f"{inst.local_symbol}:30s")
            if completed_1m < 5:
                missing.append(f"{inst.local_symbol}:1m")
            if completed_5m < 1:
                missing.append(f"{inst.local_symbol}:5m")
            if not session_ready:
                missing.append(f"{inst.local_symbol}:VWAP")

            instrument_ready = (
                inst.market_data_ok
                and bbo_ready
                and book_ready
                and tape_ready
                and bars_ready
                and session_ready
            )

            overall_ready = overall_ready and instrument_ready

            instrument_lines += [
                "",
                f"Instrument: {inst.local_symbol}",
                f"Market data: {'OK' if inst.market_data_ok else 'NOT READY'}",
                f"BBO: {'ACTIVE' if bbo_ready else 'UNAVAILABLE'}",
                f"DOM: {inst.book.state.value.upper() if inst.book else 'UNAVAILABLE'}",
                f"Tape: {'ACTIVE' if tape_ready else 'EMPTY'}",
                (
                    "Bars: "
                    f"30s {completed_30s}/10 · "
                    f"1m {completed_1m}/5 · "
                    f"5m {completed_5m}/1"
                ),
                f"VWAP/session: {'READY' if session_ready else 'UNAVAILABLE'}",
            ]

        if not connection_ok:
            context = "DISCONNECTED"
            icon = "🔴"
        elif age_ms > 5000:
            context = "STALE"
            icon = "🔴"
        elif overall_ready:
            context = "LIVE"
            icon = "🟢"
        else:
            context = "WARMING_UP"
            icon = "🟡"

        lines = [
            f"{icon} *Hermès STATUS*",
            f"IBKR: {snap.connection.value.upper()}",
            f"Uptime: {uptime_s:.0f} s",
            f"Snapshot age: {age_ms:.0f} ms",
        ]

        lines.extend(instrument_lines)

        lines += [
            "",
            f"Context: *{context}*",
            (
                "Missing: "
                + ", ".join(dict.fromkeys(missing))
                if missing
                else "Missing: none"
            ),
            "Slack: CONNECTED",
            "Execution: *DISABLED*",
            f"Git: `{git_commit()}`",
        ]

        return "\n".join(lines)

    def _slack_market_context(self) -> str:
        """Compact immutable snapshot context for conversational Slack analysis."""
        snap = self.publisher.latest()

        if snap is None:
            return "No MarketSnapshot has been published yet."

        now_mono_ns = time.perf_counter_ns()
        snapshot_age_ms = max(0.0, (now_mono_ns - snap.mono_ns) / 1_000_000)

        warmup_elapsed_s = max(
            0.0,
            (now_mono_ns - self.started_mono) / 1_000_000_000,
        )

        readiness_reasons: list[str] = []

        if not snap.instruments:
            readiness_reasons.append("no_instrument_snapshot")
        else:
            for inst in snap.instruments:
                if not inst.market_data_ok:
                    readiness_reasons.append(
                        f"{inst.local_symbol}:market_data_not_ok"
                    )

                if inst.bbo is None:
                    readiness_reasons.append(
                        f"{inst.local_symbol}:bbo_unavailable"
                    )

                if inst.book is None or inst.book.state.value != "valid":
                    readiness_reasons.append(
                        f"{inst.local_symbol}:book_not_valid"
                    )

                if inst.tape is None or inst.tape.size == 0:
                    readiness_reasons.append(
                        f"{inst.local_symbol}:tape_empty"
                    )

                if inst.bars is None:
                    readiness_reasons.append(
                        f"{inst.local_symbol}:bars_unavailable"
                    )
                else:
                    if inst.bars.completed_30s < 10:
                        readiness_reasons.append(
                            f"{inst.local_symbol}:need_30s_10_have_{inst.bars.completed_30s}"
                        )
                    if inst.bars.completed_1m < 5:
                        readiness_reasons.append(
                            f"{inst.local_symbol}:need_1m_5_have_{inst.bars.completed_1m}"
                        )
                    if inst.bars.completed_5m < 1:
                        readiness_reasons.append(
                            f"{inst.local_symbol}:need_5m_1_have_{inst.bars.completed_5m}"
                        )

                if inst.session is None:
                    readiness_reasons.append(
                        f"{inst.local_symbol}:session_unavailable"
                    )
                else:
                    st = inst.session.session
                    if st is None or not st.volume:
                        readiness_reasons.append(
                            f"{inst.local_symbol}:session_vwap_unavailable"
                        )

        if snapshot_age_ms > 5000:
            context_status = "STALE"
        elif readiness_reasons:
            context_status = "WARMING_UP"
        else:
            context_status = "LIVE"

        lines = [
            "CONTEXT_FRESHNESS:",
            f"context_status={context_status}",
            f"snapshot_age_ms={snapshot_age_ms:.1f}",
            f"warmup_elapsed_s={warmup_elapsed_s:.1f}",
            f"readiness_ready={not readiness_reasons}",
            f"readiness_reasons={readiness_reasons}",
            f"seq={snap.seq}",
            f"connection={snap.connection.value}",
            f"farm_broken={snap.farm_broken}",
            f"not_live={snap.not_live}",
            f"alerts={list(snap.alerts)}",
        ]

        def bar_line(label, b, grid):
            if b is None:
                return f"{label}=UNAVAILABLE"

            return (
                f"{label}="
                f"O:{_fmt_price(b.open, grid)} "
                f"H:{_fmt_price(b.high, grid)} "
                f"L:{_fmt_price(b.low, grid)} "
                f"C:{_fmt_price(b.close, grid)} "
                f"volume:{b.volume} "
                f"trades:{b.trades} "
                f"buy:{b.buy_volume} "
                f"sell:{b.sell_volume} "
                f"unknown:{b.unknown_volume} "
                f"delta:{b.known_delta} "
                f"flags:{b.flags.name if b.flags else 'NONE'}"
            )

        for inst in snap.instruments:
            state = self.engine.instruments.get(inst.instrument_id)
            grid = state.grid if state is not None else None

            lines += [
                "",
                f"instrument={inst.local_symbol}",
                f"con_id={inst.con_id}",
                f"contract_state={inst.contract_state}",
                f"market_data_ok={inst.market_data_ok}",
                f"not_ok_reasons={list(inst.not_ok_reasons)}",
                f"market_data_type={inst.market_data_type}",
            ]

            if inst.bbo is not None:
                lines += [
                    f"bbo_bid={_fmt_price(inst.bbo.bid_units, grid)} size={inst.bbo.bid_size}",
                    f"bbo_ask={_fmt_price(inst.bbo.ask_units, grid)} size={inst.bbo.ask_size}",
                ]
            else:
                lines.append("bbo=UNAVAILABLE")

            if inst.last_trade is not None:
                lines.append(
                    f"last_trade={_fmt_price(inst.last_trade.price_units, grid)} "
                    f"size={inst.last_trade.size}"
                )

            if inst.book is not None:
                lines += [
                    f"book_state={inst.book.state.value}",
                    f"book_bid_levels={len(inst.book.bids)}",
                    f"book_ask_levels={len(inst.book.asks)}",
                ]

                if inst.book.bids:
                    lines.append(
                        "top_book_bids="
                        + str([
                            (_fmt_price(px, grid), size)
                            for px, size in inst.book.bids[:10]
                        ])
                    )

                if inst.book.asks:
                    lines.append(
                        "top_book_asks="
                        + str([
                            (_fmt_price(px, grid), size)
                            for px, size in inst.book.asks[:10]
                        ])
                    )

            if inst.tape is not None:
                t = inst.tape.retained_window

                lines += [
                    f"tape_buy_volume={t.buy_volume}",
                    f"tape_sell_volume={t.sell_volume}",
                    f"tape_unknown_volume={t.unknown_volume}",
                    f"tape_known_delta={t.known_delta}",
                    f"last_aggressor="
                    f"{inst.tape.last_aggressor.value if inst.tape.last_aggressor else None}",
                ]

            if inst.session is not None:
                ss = inst.session
                st = ss.session

                lines += [
                    f"in_session={ss.in_trading_session}",
                    f"in_rth={ss.in_rth}",
                    f"trading_date={ss.trading_date}",
                    f"observed_from_open={ss.observed_from_open}",
                    f"session_gap_observed={ss.gap_observed}",
                ]

                if st is not None and st.volume:
                    vwap_units = round(st.vwap_num / st.volume)

                    lines += [
                        f"session_vwap={_fmt_price(vwap_units, grid)}",
                        f"session_high={_fmt_price(st.high, grid)}",
                        f"session_low={_fmt_price(st.low, grid)}",
                        f"session_volume={st.volume}",
                    ]
                else:
                    lines.append("session_vwap=UNAVAILABLE")

            if inst.bars is not None:
                b = inst.bars

                bars_ready = (
                    b.completed_30s >= 10
                    and b.completed_1m >= 5
                    and b.completed_5m >= 1
                )

                lines += [
                    "WARMUP_PROGRESS:",
                    f"bars_ready={bars_ready}",
                    f"completed_30s={b.completed_30s}/10",
                    f"completed_1m={b.completed_1m}/5",
                    f"completed_5m={b.completed_5m}/1",
                    bar_line("forming_30s", b.forming_30s, grid),
                    bar_line("forming_1m", b.forming_1m, grid),
                    bar_line("forming_5m", b.forming_5m, grid),
                    bar_line(
                        "latest_completed_30s",
                        b.latest_30s[0] if b.latest_30s else None,
                        grid,
                    ),
                    bar_line(
                        "latest_completed_1m",
                        b.latest_1m[0] if b.latest_1m else None,
                        grid,
                    ),
                    bar_line(
                        "latest_completed_5m",
                        b.latest_5m[0] if b.latest_5m else None,
                        grid,
                    ),
                ]

        return "\\n".join(lines)

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
                        "last": i.tape.last_aggressor.value if i.tape.last_aggressor else None},
                    "bars": _bars_report(i.bars, grid),
                    "session": _session_report(i.session, grid)}
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
        if self.slack_bridge is not None:
            rep["slack"] = self.slack_bridge.summary()
        elif self.slack_config_error is not None:
            rep["slack"] = {"enabled": False, "config_error": self.slack_config_error}
        else:
            rep["slack"] = {"enabled": False}
        d = self.decisions
        if d is not None:
            last = d.journal[-1] if d.journal else None     # list append is atomic; read-only view
            rep["decision"] = {"evaluations": d.driver.candidates.stats.evaluations,
                               "journal": len(d.journal), "active": len(d.driver.lifecycle.active),
                               "last": None if last is None else [last.kind.value, last.status, last.setup_id]}
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
        for i in (rep.get("instruments") or {}).values():
            bb, ss = i.get("bars"), i.get("session")
            if bb:
                parts.append(f"bars30s={bb['completed'][0]} late={bb['late'][0]} gap={bb['gap_bars']}"
                             + (f" FLAGS={bb['active_flags']}" if bb["active_flags"] else ""))
            if ss and ss["calendar_ok"]:
                parts.append(f"sess={ss['trading_date']}{'/RTH' if ss['in_rth'] else ''} vwap={ss['vwap']}")
        dec = rep.get("decision")
        if dec:
            last = dec["last"]
            parts.append(f"dec eval={dec['evaluations']} active={dec['active']}"
                         + (f" last={last[0]}:{last[1]}" if last else ""))
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
        dec = self.decision_summary(snap.seq if snap is not None else None)
        return {
            "healthy": not problems, "problems": problems, "time_to_market_data_ok_s": first_ok_s,
            "contract": inst.local_symbol if inst else None, "con_id": inst.con_id if inst else None,
            "book_state_before_shutdown": inst.book.state.value if inst and inst.book else None,
            "raw_events": self.pipeline.seq, "callbacks": self.pipeline.callbacks,
            "recording": str(self.recorder.session_dir) if self.recorder else None,
            "replay_complete": ver.replay_complete if ver else None,
            "live_checkpoints": len(self.checkpointer.checkpoints),
            "final_state_hash": self.checkpointer.final.hash if self.checkpointer.final else None,
            "connect_attempts": self.supervisor.connect_attempts,
            "connection_before_shutdown": snap.connection.value if snap is not None else None,
            "live_data_confirmed": bool(inst and inst.market_data_type == 1 and self.pipeline.ever_market_data_ok),
            "read_only_violations": violations,
            # D2.4 observability: keep the layers apart (``healthy`` above is the conjunction)
            "process_ok": not violations and not self.pipeline.internal_errors
            and (ver is None or ver.replay_complete),
            "market_data_ok_before_shutdown": bool(inst and inst.market_data_ok),
            "depth_ok_before_shutdown": bool(inst and inst.book and inst.book.state.value == "valid"),
            "warm_start": self._warm_start_line(),
            "slack": (self.slack_bridge.summary() if self.slack_bridge is not None else
                      {"enabled": False, **({"config_error": self.slack_config_error}
                                           if self.slack_config_error is not None else {})}),
            "hermes_version": HERMES_VERSION, "git_commit": git_commit(), "code_fingerprint": fp.code_fingerprint(),
            **dec,
        }

    def _warm_start_line(self) -> str:
        r = self.warm_start_result
        if r is None:
            return "not attempted"
        if not r.used:
            return f"not used ({r.reason})"
        line = (f"used: {r.source_session_id} cutoff_seq={r.cutoff_seq} raw={r.raw_events} "
                f"bars 30s/1m/5m={r.bars_30s}/{r.bars_1m}/{r.bars_5m}")
        if r.historical_alerts:
            line += " historical_alerts=" + ",".join(r.historical_alerts)
        return line

    def decision_summary(self, pre_shutdown_seq: int | None) -> dict[str, Any]:
        d = self.decisions
        if d is None:
            return {"decision_layer": "disabled"}
        c = d.counts()
        pre = [r for r in d.journal if pre_shutdown_seq is None or r.seq <= pre_shutdown_seq]
        pre_cp = [cp for cp in d.checkpoints if pre_shutdown_seq is None or cp.seq <= pre_shutdown_seq]
        return {
            "decision_evaluations": c["evaluations"],
            "decision_candidates_by_status": c["candidates_by_status"],
            "decision_transitions": c["transitions"],
            "decision_journal_records": c["journal_records"],
            "decision_checkpoints": c["decision_checkpoints"],
            "decision_final_fingerprint": d.final.fingerprint if d.final else None,
            "decision_pre_shutdown": {"seq": pre_shutdown_seq, "journal_records": len(pre),
                                      "evaluations": sum(1 for r in pre if r.kind is JournalKind.DECISION_EVALUATED),
                                      "last_checkpoint": pre_cp[-1].row() if pre_cp else None},
            "decision_file": str(self.decision_file) if self.decision_file else None,
        }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Hermès Phase C live market engine + HUMAN_APPROVAL proposals (READ-ONLY)")
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
    print(f"\n=========== HERMÈS {HERMES_VERSION} RUN SUMMARY (READ-ONLY, no orders) ===========")
    for k, v in summary.items():
        if k != "problems":
            print(f"{k:28s} {v}")
    print(f"{'RESULT':28s} {'HEALTHY' if summary['healthy'] else 'UNHEALTHY'}")
    for p in summary["problems"]:
        print(f"  ! {p}")
    return 0 if summary["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
