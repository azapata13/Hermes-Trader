"""C9f: the complete decision stack wired into the REAL live runtime (LiveRuntime / RawPipeline /
Recorder) and into replay — same DecisionRuntime code, separate market and decision checkpoints.

The live side is driven through ``RawPipeline.on_callback`` with scripted IBKR callbacks (no TWS):
sequencing, timer ticks, recording, checkpointing and the decision consumer all run exactly as in a
live session. The IbkrSession consumer is detached so it does not try to issue requests.
"""

from __future__ import annotations

import dataclasses

import pytest

from hermes.app.run_live import LiveRuntime
from hermes.config import load_config
from hermes.decision.response import HumanAction, HumanApprovalResponse, execution_prerequisites
from hermes.decision.runtime import DecisionRuntime, JournalKind
from hermes.decision.safety import approval_check
from hermes.replay.decisions import DECISIONS_SIDECAR, compare_decisions, load_decisions
from hermes.replay.runner import ReplayOptions, replay_session
from tests.support import DEPTH, TICK
from tests.unit.test_candidate import T0, trend

_SKIP = ("seq", "recv_mono_ns", "recv_wall_ns")


def live_cfg(tmp_path, decisions: bool = True):
    cfg = load_config()
    return dataclasses.replace(
        cfg,
        recorder=dataclasses.replace(cfg.recorder, directory=str(tmp_path / "rec"), flush_interval_ms=20),
        telemetry=dataclasses.replace(cfg.telemetry, log_directory=str(tmp_path / "logs"), console=False),
        decision=dataclasses.replace(cfg.decision, enabled=decisions),
    )


def scripted_live(events, tmp_path, decisions: bool = True) -> LiveRuntime:
    rt = LiveRuntime(live_cfg(tmp_path, decisions))
    rt.pipeline.consumers.remove(rt.session)          # no TWS: never issue requests
    rt.start_recording()
    for ev in events:
        fields = {f.name: getattr(ev, f.name) for f in dataclasses.fields(ev) if f.name not in _SKIP}
        rt.pipeline.on_callback(ev.recv_mono_ns, ev.recv_wall_ns, type(ev), fields)
    rt.finish()
    assert rt.pipeline.internal_errors == 0
    return rt


def kinds(journal):
    return [r.kind for r in journal]


def assert_live_replay_equivalent(rt: LiveRuntime):
    d = rt.recorder.session_dir
    assert (d / DECISIONS_SIDECAR).exists() and rt.decision_file is not None
    a = replay_session(d)
    assert a.live_compare is not None and a.live_compare.equivalent, a.live_compare_status
    assert a.decision_compare is not None and a.decision_compare.equivalent, a.decision_compare_status
    live = load_decisions(d / DECISIONS_SIDECAR)
    assert [r.row() for r in live.journal] == [r.row() for r in a.decisions.journal]
    assert live.final == a.decisions.final
    b = replay_session(d, ReplayOptions(policy=a.policy))            # second FAST replay: IDENTICAL
    assert [(c.key(), c.hash) for c in a.checkpoints] == [(c.key(), c.hash) for c in b.checkpoints]
    assert a.final_hash == b.final_hash
    assert compare_decisions(a.decision_set, b.decision_set).equivalent
    assert a.decisions.final == b.decisions.final
    return a


# ============================================================================ scenarios

def scenario_expiry():
    sc = trend(+1, minutes=12)
    return sc


def scenario_hold():
    sc = trend(+1, minutes=10)
    sc.at(T0 + 605)
    sc.depth(DEPTH, 0, 1, 1, 21010.25 + TICK * 1, 10)                 # best bid above best ask: crossed
    sc.advance(100)                                                    # < transient_grace_ms (250 ms)
    sc.depth(DEPTH, 0, 1, 1, 21010.25 - TICK * 1, 10)                 # coherent VALID book again
    for t in range(607, 640):
        sc.at(T0 + t)
        sc.tick()
    return sc


def scenario_continuity_break():
    sc = trend(+1, minutes=10)
    sc.at(T0 + 605)
    sc.error(-1, 1100)                                                 # connectivity lost
    for t in range(606, 612):
        sc.at(T0 + t)
        sc.tick()
    return sc


def scenario_c7_unavailable():
    sc = trend(+1, minutes=10)
    sc.at(T0 + 605)
    for _ in range(5):
        sc.depth(DEPTH, 0, 2, 1, 0.0, 0)                               # empty the bid side row by row
    for t in range(606, 610):
        sc.at(T0 + t)
        sc.tick()
    return sc


@pytest.fixture(scope="module")
def expiry_run(tmp_path_factory):
    return scripted_live(scenario_expiry().events, tmp_path_factory.mktemp("expiry"))


def test_live_decisions_expiry_and_replay_equivalence(expiry_run):
    rt = expiry_run
    j = rt.decisions.journal
    assert JournalKind.CANDIDATE_ACTIONABLE in kinds(j) and JournalKind.APPROVAL_VIEW_CREATED in kinds(j)
    assert JournalKind.CANDIDATE_EXPIRED in kinds(j)
    act = next(r for r in j if r.kind is JournalKind.CANDIDATE_ACTIONABLE)
    exp = next(r for r in j if r.kind is JournalKind.CANDIDATE_EXPIRED)
    assert act.setup_id == exp.setup_id and act.proposal_id == exp.proposal_id and act.proposal_id.startswith("P")
    view = next(r for r in j if r.kind is JournalKind.APPROVAL_VIEW_CREATED)
    assert view.approval_view_id.startswith("V") and view.approval_allowed_now
    a = assert_live_replay_equivalent(rt)
    c = a.decision_compare
    assert c.evaluations[0] == c.evaluations[1] > 20 and c.candidates == (c.candidates[0],) * 2
    assert c.proposal_ids_match and c.view_ids_match and c.setup_ids_match


def test_live_summary_reports_the_decision_layer(expiry_run):
    s = expiry_run.summary()
    assert s["decision_evaluations"] > 20 and s["decision_transitions"] >= 1
    assert "EXPIRED" in s["decision_candidates_by_status"] and "NONE" in s["decision_candidates_by_status"]
    assert s["decision_final_fingerprint"] == expiry_run.decisions.final.fingerprint
    assert s["decision_file"].endswith(DECISIONS_SIDECAR) and s["hermes_version"] == "0.9.0-c9"
    assert s["read_only_violations"] == 0


@pytest.mark.parametrize("make,expected", [
    (scenario_hold, [JournalKind.TEMPORARY_HOLD_ENTERED, JournalKind.TEMPORARY_HOLD_CLEARED]),
    (scenario_continuity_break, [JournalKind.CANDIDATE_BLOCKED]),
    (scenario_c7_unavailable, [JournalKind.CANDIDATE_BLOCKED]),
])
def test_lifecycle_scenarios_live_vs_replay(make, expected, tmp_path):
    rt = scripted_live(make().events, tmp_path)
    ks = kinds(rt.decisions.journal)
    act = ks.index(JournalKind.CANDIDATE_ACTIONABLE)
    for k in expected:
        assert k in ks[act:], (k, ks)
    if make is scenario_hold:
        entered = next(r for r in rt.decisions.journal if r.kind is JournalKind.TEMPORARY_HOLD_ENTERED)
        assert ("HOLD", "lifecycle", "crossed_book_transition") in entered.reasons
        assert not entered.approval_allowed_now
    if make is scenario_c7_unavailable:
        blocked = [r for r in rt.decisions.journal[act:] if r.kind is JournalKind.CANDIDATE_BLOCKED][0]
        assert ("BLOCK", "lifecycle", "c7_metrics_unavailable") in blocked.reasons
    if make is scenario_continuity_break:
        blocked = [r for r in rt.decisions.journal[act:] if r.kind is JournalKind.CANDIDATE_BLOCKED][0]
        assert ("BLOCK", "lifecycle", "connection_unusable") in blocked.reasons
    assert_live_replay_equivalent(rt)


def test_decision_consumer_does_not_alter_market_state(tmp_path):
    ev = scenario_hold().events
    on = scripted_live(ev, tmp_path / "on", decisions=True)
    off = scripted_live(ev, tmp_path / "off", decisions=False)
    assert on.decisions is not None and off.decisions is None
    assert [(c.key(), c.hash) for c in on.checkpointer.checkpoints] == \
        [(c.key(), c.hash) for c in off.checkpointer.checkpoints]
    assert on.checkpointer.final.hash == off.checkpointer.final.hash
    assert on.engine.snapshot() == off.engine.snapshot()


def test_replay_uses_the_same_decision_runtime(expiry_run):
    a = replay_session(expiry_run.recorder.session_dir)
    assert type(a.decisions) is DecisionRuntime and type(expiry_run.decisions) is DecisionRuntime
    off = replay_session(expiry_run.recorder.session_dir, ReplayOptions(decisions=False))
    assert off.decisions is None and off.final_hash == a.final_hash       # market result independent


def test_enter_on_a_live_view_still_cannot_execute(expiry_run):
    rt = expiry_run
    view = rt.decisions.last_view
    assert view is not None and view.approval_allowed_now
    resp = HumanApprovalResponse(HumanAction.ENTER, view.setup_id, view.proposal_id, view.approval_view_id,
                                 response_wall_ns=view.safety_evaluated_wall_ns)
    rec = rt.decisions.driver.lifecycle.get(view.setup_id)
    pre = execution_prerequisites(resp, rec, approval_check(rt.engine, rec, now_wall_ns=view.safety_evaluated_wall_ns))
    assert not pre.satisfied and not resp.authorizes_execution
    assert {"risk_eligibility_policy_not_implemented", "execution_layer_disabled"} <= set(pre.codes)


def test_approval_view_id_stable_for_same_event_state(expiry_run):
    from hermes.decision.approval import current_approval_payload
    rt = expiry_run
    view = rt.decisions.last_view
    rec = rt.decisions.driver.lifecycle.get(view.setup_id)
    a = current_approval_payload(rt.engine, rec, now_wall_ns=view.safety_evaluated_wall_ns)
    b = current_approval_payload(rt.engine, rec, now_wall_ns=view.safety_evaluated_wall_ns)
    assert a.approval_view_id == b.approval_view_id and a.proposal_id == view.proposal_id
