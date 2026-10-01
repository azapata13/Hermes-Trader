"""Warm start reconstructs market HISTORY, never the previous process's live HEALTH state.

Regression (D1.9 live validation): a recording that ended after ``depth_resync_exhausted`` was
replayed by the warm start, re-raising ``depth_resync_budget_exhausted`` in the new process. The
only thing that clears that alert is the *same session's* budget reset, which a new process never
posts, so the alert survived a fully healthy live reconnection: market data stayed "not OK",
SafetyPolicy kept a ``critical_alert`` and every proposal was BLOCKED. Each failed run then became
the next warm-start recording, so the state perpetuated itself.

These tests pin both sides of the fix:
- alerts / 10197 recovery replayed from the recording are reported as HISTORICAL and are not live
  state after the warm start;
- a NEW live alert (raised by the live session after warm start) is still raised and still blocks.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from hermes.config import BookConfig, SessionConfig, SubscriptionsConfig
from hermes.ibkr import raw_events as R
from hermes.ibkr.normalizer import Normalizer
from hermes.market.engine import (
    ALERT_CONFLICT_EXHAUSTED,
    ALERT_DEPTH_RESYNC_EXHAUSTED,
    MarketEngine,
)
from hermes.market.events import ConnectionState
from hermes.market.health import ConflictPhase
from hermes.replay.warm_start import warm_start_engine
from tests.support import MS, SPEC, RawScript, write_hrec

T0_WALL = 1_790_000_000 * 10**9


def _engine() -> MarketEngine:
    return MarketEngine(BookConfig(), SessionConfig(), SubscriptionsConfig())


def _failed_session(kind: str) -> RawScript:
    """A recorded session that ended with a critical alert, then the connection closed."""
    s = RawScript(wall0=T0_WALL)
    s.bootstrap().seed_book()
    s.advance(600).tick()
    for i in range(5):                                   # some history worth warm-starting
        s.advance(1000)
        s.trade(10_003, 21000.25 if i % 2 else 21000.0)
    if kind == "depth":
        s.control("depth_resync_exhausted", "5 resyncs within 300.0s")
    elif kind == "conflict":
        for _ in range(SessionConfig().conflict_max_attempts + 1):
            s.error(-1, 10197, "No market data during competing live session")
            s.advance(10).control("conflict_recovery_attempt")
            s.advance(SessionConfig().recovery_attempt_timeout_s * 1000 + 10).tick()
    s.advance(100).closed()
    return s


def _record(tmp_path, script: RawScript):
    root = tmp_path / "recordings"
    write_hrec(root / "2026-10-01" / "20261001T130000Z-1", script.events,
               meta={"contract_spec": dict(SPEC.to_params())})
    return root


def _warm(tmp_path, kind: str, engine: MarketEngine, now_mono_ns: int):
    root = _record(tmp_path, _failed_session(kind))
    with mock.patch.dict(os.environ, {"HERMES_WARM_START": "1"}):
        return warm_start_engine(engine, root, expected_contract_spec=dict(SPEC.to_params()),
                                 max_age_s=10**9, now_mono_ns=now_mono_ns)


def _live(engine: MarketEngine, mono0: int, *, extra=None) -> RawScript:
    """A NEW process' live session (fresh normalizer, seq restarting at 1) that is fully healthy."""
    s = RawScript(mono0=mono0, wall0=T0_WALL + 3600 * 10**9)
    s.control("connect_attempt")
    s.bootstrap().seed_book()
    s.advance(600).tick()
    if extra is not None:
        extra(s)
    n = Normalizer()
    for raw in s.events:
        for ev in n.normalize(raw):
            engine.on_event(ev)
    return s


def _reasons(engine: MarketEngine) -> list[str]:
    return engine.market_data_reasons(engine.instruments[1])


@pytest.mark.parametrize("kind,alert", [("depth", ALERT_DEPTH_RESYNC_EXHAUSTED),
                                        ("conflict", ALERT_CONFLICT_EXHAUSTED)])
def test_replayed_alert_is_historical_not_live(tmp_path, kind, alert):
    eng = _engine()
    res = _warm(tmp_path, kind, eng, now_mono_ns=50_000 * MS)
    assert res.used, res.reason
    # the alert WAS in the recording and is reported, never silently dropped
    assert alert in res.historical_alerts
    # ...but it is not live state of the new process
    assert alert not in eng.alerts and eng.new_alerts == []
    assert eng.connection is ConnectionState.DISCONNECTED
    assert eng.conflict.phase is ConflictPhase.NONE and eng.conflict.attempts == 0
    # history is preserved (the tape survives; only health state was reset)
    assert res.tape_size == 5 and len(eng.instruments[1].tape) == 5
    # replayed book / subscriptions are not live: nothing can pass for a live stream
    assert all(st.generation is None for st in eng.instruments[1].streams.values())
    assert eng.instruments[1].book.state.value != "valid"
    # a healthy live reconnection is then genuinely usable
    _live(eng, mono0=10**15)
    assert _reasons(eng) == []
    assert eng.snapshot().instrument(1).market_data_ok


def test_without_fix_semantics_documented_replayed_alert_would_block(tmp_path):
    """The recording really does produce the alert when replayed verbatim (guards the repro)."""
    eng = _engine()
    n = Normalizer()
    for raw in _failed_session("depth").events:
        if isinstance(raw, R.RawConnectionClosed):
            break
        for ev in n.normalize(raw):
            eng.on_event(ev)
    assert ALERT_DEPTH_RESYNC_EXHAUSTED in eng.alerts


def test_new_live_depth_alert_after_warm_start_still_blocks(tmp_path):
    eng = _engine()
    _warm(tmp_path, "depth", eng, now_mono_ns=50_000 * MS)
    _live(eng, mono0=10**15,
          extra=lambda s: s.advance(100).control("depth_resync_exhausted", "5 resyncs within 300.0s"))
    assert ALERT_DEPTH_RESYNC_EXHAUSTED in eng.alerts
    assert ALERT_DEPTH_RESYNC_EXHAUSTED in eng.new_alerts          # surfaced as a NEW live alert
    assert f"alert:{ALERT_DEPTH_RESYNC_EXHAUSTED}" in _reasons(eng)
    assert not eng.snapshot().instrument(1).market_data_ok


def test_new_live_conflict_after_warm_start_still_blocks(tmp_path):
    eng = _engine()
    _warm(tmp_path, "conflict", eng, now_mono_ns=50_000 * MS)
    _live(eng, mono0=10**15,
          extra=lambda s: s.advance(10).error(-1, 10197, "No market data during competing live session"))
    assert eng.conflict.active
    assert any(r.startswith("conflict_10197:") for r in _reasons(eng))
    assert not eng.snapshot().instrument(1).market_data_ok


def test_clean_recording_has_no_historical_alerts(tmp_path):
    eng = _engine()
    res = _warm(tmp_path, "clean", eng, now_mono_ns=50_000 * MS)
    assert res.used and res.historical_alerts == () and res.historical_conflict_phase == "none"
    _live(eng, mono0=10**15)
    assert _reasons(eng) == []


def test_replayed_book_is_not_live_before_the_new_session_subscribes(tmp_path):
    """Second half of the same defect: after a warm start, a NEW connection that has resolved the
    contract but not yet re-subscribed must not report usable market data from the recording's
    old book / generations (previously market_data_reasons() was empty in that window)."""
    from tests.support import CONTRACT
    eng = _engine()
    _warm(tmp_path, "clean", eng, now_mono_ns=50_000 * MS)
    s = RawScript(mono0=10**15, wall0=T0_WALL + 3600 * 10**9)
    s.control("connect_attempt")
    s.next_valid_id()
    s.request("reqMarketDataType", None, iid=0, market_data_type=1)
    s.request("reqContractDetails", CONTRACT, **dict(SPEC.to_params()))
    s.contract_details()
    s.contract_end()
    s.request("reqMarketRule", None, rule_id=67)
    s.market_rule()
    s.advance(600).tick()
    n = Normalizer()
    for raw in s.events:
        for ev in n.normalize(raw):
            eng.on_event(ev)
    assert eng.connection is ConnectionState.CONNECTED
    r = _reasons(eng)
    assert "depth:not_subscribed" in r and "bbo:not_subscribed" in r and "l1:not_subscribed" in r
    assert not eng.snapshot().instrument(1).market_data_ok


# ---------------------------------------------------------------------------
# D2.4: fail-closed replay failure and deterministic re-application
# ---------------------------------------------------------------------------

def test_warm_replay_failure_fails_closed(tmp_path, monkeypatch):
    """A processing error half-way through the warm replay must not leave the replayed connection,
    generations or book in place (they would pass for live state before the new session starts)."""
    eng = _engine()
    calls = {"n": 0}
    real = eng.on_event

    def flaky(ev):
        calls["n"] += 1
        if calls["n"] == 20:
            raise RuntimeError("boom")
        real(ev)

    monkeypatch.setattr(eng, "on_event", flaky)
    res = _warm(tmp_path, "depth", eng, now_mono_ns=50_000 * MS)
    assert not res.used and "warm replay failed" in res.reason and res.applied_events > 0
    assert eng.connection is ConnectionState.DISCONNECTED and eng.alerts == {}
    if 1 in eng.instruments:
        assert all(st.generation is None for st in eng.instruments[1].streams.values())


def test_reapplied_warm_start_is_identical(tmp_path):
    """Same source, cutoff and monotonic base -> byte-identical starting state (what replay relies on)."""
    from hermes.replay.warm_start import apply_recorded_warm_start
    live = _engine()
    res = _warm(tmp_path, "depth", live, now_mono_ns=50_000 * MS)
    root = tmp_path / "recordings"
    prov = res.provenance(root)
    assert prov["source_relpath"] == "2026-10-01/20261001T130000Z-1"
    new_session = root / "2026-10-01" / "20261001T140000Z-2"
    new_session.mkdir(parents=True)
    rep = _engine()
    ok, note = apply_recorded_warm_start(rep, new_session, prov)
    assert ok, note
    assert rep.snapshot() == live.snapshot() and rep.state_token() == live.state_token()


@pytest.mark.parametrize("tamper", ["session_id", "base", "missing"])
def test_reapplication_refuses_a_source_that_does_not_match(tmp_path, tamper):
    from hermes.replay.warm_start import apply_recorded_warm_start
    res = _warm(tmp_path, "clean", _engine(), now_mono_ns=50_000 * MS)
    root = tmp_path / "recordings"
    prov = res.provenance(root)
    if tamper == "session_id":
        prov["source_session_id"] = "someone-else"
    elif tamper == "base":
        prov["base_mono_ns"] = None
    else:
        prov["source_relpath"], prov["source_dir"] = "2026-10-01/gone", str(tmp_path / "gone")
    ok, note = apply_recorded_warm_start(_engine(), root / "2026-10-01" / "x", prov)
    assert not ok and note


def test_recording_metadata_is_frozen_once_started(tmp_path):
    from hermes.config import RecorderConfig
    from hermes.storage.recorder import Recorder
    rec = Recorder(RecorderConfig(directory=str(tmp_path)), {"a": 1})
    rec.set_meta("warm_start", {"used": False})
    rec.start()
    try:
        with pytest.raises(RuntimeError):
            rec.set_meta("late", 1)
    finally:
        rec.stop()


def test_warm_history_never_makes_a_candidate_actionable_without_live_depth(tmp_path):
    """Scenario F after a warm start: the replayed history alone (11 minutes of a clean trend,
    enough bars for every timeframe) must not let the decision layer produce an ACTIONABLE
    candidate while the NEW live session has no valid DOM. Evaluations do run (non-vacuous) and
    are BLOCKED by the safety policy with the market-data reasons."""
    from hermes.decision.runtime import DecisionRuntime, JournalKind
    from tests.support import BBO, TICK, TRADES, Harness
    from tests.unit.test_candidate import WEEK, trend

    rec = trend(+1, minutes=11)
    root = tmp_path / "recordings"
    write_hrec(root / "2026-10-01" / "s1", rec.events, meta={"contract_spec": dict(SPEC.to_params())})
    h = Harness()
    with mock.patch.dict(os.environ, {"HERMES_WARM_START": "1"}):
        res = warm_start_engine(h.engine, root, expected_contract_spec=dict(SPEC.to_params()),
                                max_age_s=10**12, now_mono_ns=10**12)
    assert res.used and res.bars_1m >= 10
    rt = DecisionRuntime(h.engine)
    s = RawScript(mono0=10**15, wall0=rec.events[-1].recv_wall_ns + 5 * 10**9)
    s.control("connect_attempt")
    s.bootstrap(**WEEK)
    bid = 21000.0 + TICK * 75
    s.bbo(BBO, bid, bid + TICK)
    s.mdt()                                          # LIVE quotes and prints, but depth never arrives
    for k in range(40):
        s.advance(5000)
        if k % 3 == 0:
            bid += TICK
            s.bbo(BBO, bid, bid + TICK)
        s.trade(TRADES, bid + TICK, 3)
        s.tick()
    n = Normalizer()
    for raw in s.events:
        for ev in n.normalize(raw):
            h.engine.on_event(ev)
        rt.after_event(raw, (), raw.recv_mono_ns)
    evals = [r for r in rt.journal if r.kind is JournalKind.DECISION_EVALUATED]
    assert evals, "the decision layer must have evaluated (otherwise this test proves nothing)"
    assert not any(r.kind in (JournalKind.CANDIDATE_ACTIONABLE, JournalKind.APPROVAL_VIEW_CREATED)
                   for r in rt.journal)
    assert rt.last_view is None
    for r in evals:
        codes = {c for (_sev, src, c) in r.reasons if src == "safety"}
        assert r.status == "BLOCKED" and {"book_not_valid", "market_data_not_ok"} <= codes, r
