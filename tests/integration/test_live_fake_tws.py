"""End-to-end: the real live runtime against a fake TWS speaking the ibapi wire protocol.

Exercises the official path end to end: ReadOnlyClient.connect handshake, ibapi EReader +
EClient.run dispatch thread, decoder, IbkrAdapter, RawPipeline, Recorder, Normalizer,
MarketEngine, IbkrSession (subscriptions, resync, 10197 recovery), Supervisor (reconnect).
Then verifies the recording and live/replay equivalence.
"""

from __future__ import annotations

import dataclasses
import threading
import time

import pytest

from hermes.app.run_live import LiveRuntime
from hermes.config import load_config
from hermes.ibkr import codes
from hermes.market.health import ConflictPhase
from hermes.market.orderbook import BookState
from hermes.storage.reader import iter_raw_events, verify_session
from tests.fake_tws import FakeTws
from tests.support import Harness

ALLOWED_OUT = {71, 49, 59, 9, 91, 10, 11, 97, 98, 1, 2}   # startApi, time, mdt, contract, rule, depth, tbt, mktdata (+cancels)


def fast_cfg(port: int, tmp_path, **session):
    cfg = load_config()
    s = dict(conflict_retry_interval_s=0.3, recovery_attempt_timeout_s=1.0, resync_min_interval_s=0.2,
             reconnect_initial_backoff_s=0.2, reconnect_max_backoff_s=0.5, contract_timeout_s=3.0,
             market_rule_timeout_s=3.0, shutdown_grace_s=0.1)
    s.update(session)
    return dataclasses.replace(
        cfg,
        ibkr=dataclasses.replace(cfg.ibkr, port=port, heartbeat_interval_ms=250, connect_timeout_s=3.0),
        book=dataclasses.replace(cfg.book, settle_ms=100),
        session=dataclasses.replace(cfg.session, **s),
        recorder=dataclasses.replace(cfg.recorder, directory=str(tmp_path / "rec"), flush_interval_ms=20),
        telemetry=dataclasses.replace(cfg.telemetry, report_interval_s=0.5, log_directory=str(tmp_path / "logs"),
                                      console=False, snapshot_interval_ms=20),
    )


class Run:
    def __init__(self, rt: LiveRuntime, duration: float):
        self.rt = rt
        self.summary = None
        self.thread = threading.Thread(target=self._go, args=(duration,), daemon=True)
        self.thread.start()

    def _go(self, duration):
        self.summary = self.rt.run(duration, install_signals=False)

    def join(self, stop: bool = False):
        if stop:
            self.rt.supervisor.request_stop("test done")
        self.thread.join(30)
        assert not self.thread.is_alive()
        return self.summary


def wait_for(pred, timeout=8.0, what="condition", poll=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if pred():
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(poll)
    raise AssertionError(f"timed out waiting for {what}")


def md_ok(rt):
    s = rt.publisher.latest()
    return s is not None and s.instruments and s.instruments[0].market_data_ok


@pytest.fixture
def tws():
    t = FakeTws()
    yield t
    t.close()


def assert_no_forbidden_messages(tws):
    ids = set(tws.received_base_ids())
    assert not (ids & codes.FORBIDDEN_OUTGOING_MSG_IDS), ids
    assert ids <= ALLOWED_OUT, ids - ALLOWED_OUT


def assert_replay_equivalent(rt):
    ver = verify_session(rt.recorder.session_dir)
    assert ver.replay_complete, ver.problems
    h = Harness(rt.cfg.book, rt.cfg.session, rt.cfg.subscriptions, rt.cfg.tape, rt.cfg.bars).feed(iter_raw_events(rt.recorder.session_dir))
    assert h.engine.snapshot() == rt.engine.snapshot()        # deterministic live/replay equivalence
    return ver


def test_healthy_session_records_and_replays(tws, tmp_path):
    rt = LiveRuntime(fast_cfg(tws.port, tmp_path))
    run = Run(rt, 2.5)
    summary = run.join()
    assert summary["healthy"], summary["problems"]
    assert summary["contract"] == "MNQZ6" and summary["book_state_before_shutdown"] == "valid"
    assert summary["time_to_market_data_ok_s"] is not None
    assert_no_forbidden_messages(tws)
    ver = assert_replay_equivalent(rt)
    assert ver.counts_by_type["RawRequestIssued"] >= 7 and ver.counts_by_type["RawTimerTick"] >= 1
    rep = rt.reporter.last_report
    assert rep["readonly_violations"] == 0 and rep["pipeline"]["internal_errors"] == 0
    # C4: the fake's AllLast print at the bid is classified SELL from the prevailing BidAsk quote
    t = rt.engine.instruments[1].tape.trades()
    assert t and t[0].aggressor.value == "sell" and t[0].method.value == "direct_quote"
    # C5: contract tradingHours/liquidHours arrive over the wire and parse into a valid calendar;
    # the print is on a forming 30 s bar (replay equivalence above covers bars + session state)
    inst = rt.engine.snapshot().instruments[0]
    assert inst.session.calendar_ok and inst.session.time_zone == "US/Central"
    assert inst.bars is not None and (inst.bars.forming_30s is not None or inst.bars.completed_30s >= 1)


def test_317_resync_and_late_old_generation_callbacks(tws, tmp_path):
    """Old-generation callbacks never touch the current book, and the published snapshot never
    lags a state transition. Polls at 1 ms on purpose: reacting within the snapshot cadence used
    to expose a stale 'market_data_ok' snapshot with the pre-resync book (macOS regression)."""
    fast = 0.001
    rt = LiveRuntime(fast_cfg(tws.port, tmp_path))
    run = Run(rt, 8.0)
    wait_for(lambda: md_ok(rt), what="market data ok", poll=fast)
    old_depth = tws.requests["reqMktDepth"][0]
    # 317: IBKR resets depth, then re-sends the book for the same reqId
    tws.error(old_depth, 317, "Market depth data has been RESET. Please empty deep book contents")
    wait_for(lambda: rt.publisher.latest().instruments[0].book.state is not BookState.VALID,
             what="317 handled", poll=fast)
    tws.seed_depth(old_depth)
    wait_for(lambda: md_ok(rt), what="rebuild after 317", poll=fast)
    epoch_after_317 = rt.publisher.latest().instruments[0].book.epoch
    # structural violation -> STALE -> session resyncs depth with a NEW reqId
    tws.depth(old_depth, 9, 1, 1, 21000.0, 1)         # update at a non-existent row
    wait_for(lambda: not md_ok(rt), what="violation visible in the published snapshot", poll=fast)
    wait_for(lambda: len(tws.requests["reqMktDepth"]) == 2, what="depth resync", poll=fast)
    new_depth = tws.requests["reqMktDepth"][1]
    assert new_depth != old_depth
    wait_for(lambda: md_ok(rt), what="valid after resync", poll=fast)
    snap = rt.publisher.latest()
    inst = snap.instruments[0]
    depth_stream = next(st for st in inst.streams if st.stream.value == "depth")
    # the OK snapshot must describe the NEW generation's book, never the pre-resync one
    assert depth_stream.generation == new_depth
    assert inst.book.epoch == epoch_after_317 + 1 and inst.book.state is BookState.VALID
    before = inst.book
    # late callbacks from the OLD generation must not touch the book
    for _ in range(5):
        tws.depth(old_depth, 0, 0, 1, 20000.0, 999)
    tws.error(old_depth, 317, "late reset on old reqId")
    wait_for(lambda: rt.engine.counters.inactive_callbacks >= 5
             and rt.engine.counters.errors_by_code.get(317, 0) == 2, what="late callbacks processed", poll=fast)
    target_seq = rt.engine.last_seq
    wait_for(lambda: rt.publisher.latest().seq >= target_seq, what="snapshot covers late callbacks", poll=fast)
    after = rt.publisher.latest().instruments[0].book
    assert after.bids == before.bids and after.asks == before.asks
    assert after.epoch == before.epoch and after.state is BookState.VALID
    assert (80000, 999) not in after.bids                        # 20000.00 x 999 from the old reqId
    run.join(stop=True)
    assert_no_forbidden_messages(tws)
    assert_replay_equivalent(rt)


def test_10197_recovery_is_proven_and_clears(tws, tmp_path):
    rt = LiveRuntime(fast_cfg(tws.port, tmp_path))
    run = Run(rt, 6.0)
    wait_for(lambda: md_ok(rt), what="market data ok")
    tws.error(-1, 10197, "No market data during competing live session")
    wait_for(lambda: rt.engine.conflict.phase is not ConflictPhase.NONE, what="conflict detected")
    wait_for(lambda: not md_ok(rt), what="market data blocked by 10197")
    wait_for(lambda: len(tws.requests["reqMktData"]) >= 2, what="recovery attempt resubscribed")
    wait_for(lambda: rt.engine.conflict.phase is ConflictPhase.NONE and md_ok(rt), what="recovery proven")
    assert rt.engine.conflict.recoveries == 1
    run.join(stop=True)
    assert_replay_equivalent(rt)


def test_10197_retry_budget_exhausts_and_stops(tws, tmp_path):
    rt = LiveRuntime(fast_cfg(tws.port, tmp_path))
    run = Run(rt, 7.0)
    wait_for(lambda: md_ok(rt), what="market data ok")
    tws.conflict_mode = True
    tws.error(-1, 10197, "No market data during competing live session")
    wait_for(lambda: rt.engine.conflict.phase is ConflictPhase.EXHAUSTED, timeout=10, what="budget exhausted")
    n = len(tws.requests["reqMktData"])
    assert n == 1 + 3                                   # initial + exactly 3 bounded attempts
    time.sleep(1.0)
    assert len(tws.requests["reqMktData"]) == n         # no more automatic retries
    assert "market_data_conflict_retries_exhausted" in rt.engine.alerts
    summary = run.join(stop=True)
    assert not summary["healthy"]
    assert_replay_equivalent(rt)


def test_reconnect_after_connection_drop(tws, tmp_path):
    rt = LiveRuntime(fast_cfg(tws.port, tmp_path))
    run = Run(rt, 6.0)
    wait_for(lambda: md_ok(rt), what="market data ok")
    tws.drop_connection()
    wait_for(lambda: tws.connections == 2, what="reconnect")
    wait_for(lambda: md_ok(rt), what="market data ok after reconnect")
    assert len(tws.requests["reqContractDetails"]) == 1   # contract not re-resolved
    assert len(tws.requests["reqMktDepth"]) == 2          # resubscribed with a new generation
    summary = run.join(stop=True)
    assert summary["connect_attempts"] == 2
    assert_replay_equivalent(rt)


def test_connect_refused_is_unhealthy_and_retries(tmp_path):
    rt = LiveRuntime(fast_cfg(1, tmp_path))            # nothing listens on port 1
    summary = Run(rt, 1.5).join()
    assert not summary["healthy"] and "market data never became OK" in summary["problems"]
    assert summary["connect_attempts"] >= 2
