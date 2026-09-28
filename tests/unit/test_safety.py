"""C9d SafetyPolicy: one deterministic policy for creation, lifecycle and approval (no overrides)."""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from hermes.config import ConfigError, DecisionConfig, config_from_mapping
from hermes.decision import safety as S_
from hermes.decision.approval import approval_payload
from hermes.decision.context import build_decision_context
from hermes.decision.lifecycle import CandidateStatus
from hermes.decision.safety import (
    HARD_CODES,
    HOLD_CODES,
    PURPOSE_APPROVAL,
    PURPOSE_CREATION,
    PURPOSE_LIFECYCLE,
    SafetyPolicy,
    approval_check,
    facts_from_context,
    facts_from_engine,
)
from tests.support import DEPTH, TICK
from tests.unit.test_candidate import T0
from tests.unit.test_lifecycle import started

S = 1_000_000_000


@pytest.fixture(scope="module")
def base():
    lv, rec = started()
    snap = lv.h.engine.snapshot()
    ctx = build_decision_context(snap, DecisionConfig())
    return lv, rec, ctx, facts_from_context(ctx)


def ev(f, cfg=None, **kw):
    return SafetyPolicy(cfg).evaluate(f, **kw)


# ============================================================================ stable codes / no override

def test_reason_codes_are_stable():
    assert HARD_CODES == {
        "connection_unusable", "contract_not_defined", "market_data_not_live", "session_conflict_10197",
        "critical_alert", "farm_broken", "required_stream_inactive", "stream_generation_changed",
        "book_not_valid", "active_data_gap", "continuity_epoch_changed", "classification_context_invalid",
        "price_grid_not_uniform", "session_calendar_invalid", "outside_authorized_entry_hours",
        "within_opening_buffer", "within_closing_buffer", "market_data_not_ok", "c7_metrics_unavailable",
        "c8_structure_unavailable", "c8_patterns_unavailable", "candidate_expired",
        "candidate_status_not_actionable"}
    assert HOLD_CODES == {"crossed_book_transition", "unsorted_book_transition", "empty_side_transition"}
    assert not HARD_CODES & HOLD_CODES


def test_policy_accepts_no_override_input():
    params = set(inspect.signature(SafetyPolicy.evaluate).parameters)
    assert params == {"self", "f", "purpose", "baseline", "record"}


# ============================================================================ healthy baseline

def test_healthy_state_is_allowed(base):
    _, _, ctx, f = base
    r = ev(f)
    assert r.allowed and not r.hard_block_reasons and not r.temporary_hold_reasons
    assert r.session_policy.authorized and r.session_policy.policy == "RTH_ONLY"
    assert r.session_policy.opening_buffer_s == 0 and r.session_policy.closing_buffer_s == 0
    assert dict(r.market_data)["book_coherent"] == "True"


def test_engine_and_context_readers_agree(base):
    """Anti-drift: the same state judged from the snapshot/context and from the live engine."""
    lv, _, ctx, f = base
    fe = facts_from_engine(lv.h.engine, lv.h.engine.instruments[1], ctx.wall_ns)
    assert ev(fe) == ev(f)
    assert fe.continuity == f.continuity and fe.streams == f.streams and fe.required_streams == f.required_streams


# ============================================================================ each hard block

@pytest.mark.parametrize("change,code", [
    (dict(connection="lost"), "connection_unusable"),
    (dict(contract_state="pending"), "contract_not_defined"),
    (dict(market_data_type=3), "market_data_not_live"),
    (dict(not_live=True), "market_data_not_live"),
    (dict(conflict_active=True, conflict_phase="blocked"), "session_conflict_10197"),
    (dict(alerts=("internal_error",)), "critical_alert"),
    (dict(farm_broken=True), "farm_broken"),
    (dict(book_state="suspect"), "book_not_valid"),
    (dict(book_state="stale", book_coherent=False, book_coherence_reason="empty_side_transition"), "book_not_valid"),
    (dict(bar_active_flags=8), "active_data_gap"),
    (dict(classification_ok=False, classification_reason="bbo:requested"), "classification_context_invalid"),
    (dict(grid_uniform=False), "price_grid_not_uniform"),
    (dict(calendar_ok=False), "session_calendar_invalid"),
    (dict(in_rth=False), "outside_authorized_entry_hours"),
    (dict(market_data_ok=False, not_ok_reasons=("depth:requested",)), "market_data_not_ok"),
    (dict(c7_available=False), "c7_metrics_unavailable"),
    (dict(c8_structure_available=False), "c8_structure_unavailable"),
    (dict(c8_patterns_available=False), "c8_patterns_unavailable"),
])
def test_each_hard_block(base, change, code):
    _, _, _, f = base
    r = ev(dataclasses.replace(f, **change))
    assert not r.allowed and code in r.hard_codes


def test_required_stream_inactive(base):
    _, _, _, f = base
    streams = tuple((n, g, "unavailable" if n == "bbo" else st) for n, g, st in f.streams)
    r = ev(dataclasses.replace(f, streams=streams))
    assert "required_stream_inactive" in r.hard_codes
    quiet = tuple((n, g, "requested" if n == "trades" else st) for n, g, st in f.streams)
    assert ev(dataclasses.replace(f, streams=quiet)).allowed       # a quiet trades stream is legitimate


@pytest.mark.parametrize("purpose", [PURPOSE_CREATION, PURPOSE_LIFECYCLE, PURPOSE_APPROVAL])
@pytest.mark.parametrize("field,code", [
    ("c7_available", "c7_metrics_unavailable"),
    ("c8_structure_available", "c8_structure_unavailable"),
    ("c8_patterns_available", "c8_patterns_unavailable"),
])
def test_c7_c8_availability_is_judged_at_every_purpose(base, purpose, field, code):
    """Current evidence availability is independent of continuity epochs (identical epochs here)."""
    _, rec, _, f = base
    off = dataclasses.replace(f, **{field: False})
    assert off.continuity == f.continuity
    r = ev(off, purpose=purpose, baseline=None if purpose == PURPOSE_CREATION else rec.candidate,
           record=rec if purpose == PURPOSE_APPROVAL else None)
    assert not r.allowed and code in r.hard_codes and "continuity_epoch_changed" not in r.hard_codes


# ============================================================================ temporary holds

def test_crossed_valid_book_is_a_hold_not_a_block(base):
    _, _, _, f = base
    r = ev(dataclasses.replace(f, book_coherent=False, book_coherence_reason="crossed_book_transition"))
    assert not r.allowed and not r.hard_block_reasons
    assert [h.code for h in r.temporary_hold_reasons] == ["crossed_book_transition"]


def test_rows_coherence():
    ok = S_.rows_coherence(((100, 1), (99, 1)), ((101, 1), (102, 1)))
    assert ok == (True, "ok")
    assert S_.rows_coherence(((101, 1),), ((101, 1),))[1] == "crossed_book_transition"
    assert S_.rows_coherence(((100, 1), (100, 1)), ((101, 1),))[1] == "unsorted_book_transition"
    assert S_.rows_coherence((), ((101, 1),))[1] == "empty_side_transition"


# ============================================================================ session window / buffers

def test_opening_and_closing_buffers(base):
    _, _, _, f = base
    start, end = f.rth_start_s, f.rth_end_s
    early = dataclasses.replace(f, now_wall_ns=(start + 30) * S)
    assert ev(early).allowed                                         # default buffers: zero
    r = ev(early, DecisionConfig(opening_buffer_seconds=60))
    assert "within_opening_buffer" in r.hard_codes and r.session_policy.authorized_from_s == start + 60
    late = dataclasses.replace(f, now_wall_ns=(end - 30) * S)
    assert ev(late).allowed
    r2 = ev(late, DecisionConfig(closing_buffer_seconds=60))
    assert "within_closing_buffer" in r2.hard_codes and r2.session_policy.authorized_until_s == end - 60
    after = dataclasses.replace(f, now_wall_ns=end * S)
    assert "outside_authorized_entry_hours" in ev(after).hard_codes
    assert r.only_window_blocks and r2.only_window_blocks


def test_buffer_config_validation():
    assert config_from_mapping({}).decision.opening_buffer_seconds == 0
    assert config_from_mapping({}).decision.closing_buffer_seconds == 0
    with pytest.raises(ConfigError):
        config_from_mapping({"decision": {"opening_buffer_seconds": -1}})
    with pytest.raises(ConfigError):
        config_from_mapping({"decision": {"entry_policy": "ALWAYS"}})


# ============================================================================ baseline continuity / approval

def test_baseline_epoch_and_generation_changes_block(base):
    _, rec, _, f = base
    cont = tuple((n, (v or 0) + 1 if n == "structure_epoch" else v) for n, v in f.continuity)
    r = ev(dataclasses.replace(f, continuity=cont), purpose=PURPOSE_LIFECYCLE, baseline=rec.candidate)
    assert "continuity_epoch_changed" in r.hard_codes
    assert any(name == "structure_epoch" and before != now for name, before, now in r.continuity)
    gens = tuple((n, (g or 0) + 100 if n == "trades" else g, st) for n, g, st in f.streams)
    r2 = ev(dataclasses.replace(f, streams=gens), purpose=PURPOSE_LIFECYCLE, baseline=rec.candidate)
    assert "stream_generation_changed" in r2.hard_codes


def test_approval_check_on_live_engine(base):
    lv, rec, _, _ = base
    r = approval_check(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=(T0 + 605) * S)
    assert r.purpose == PURPOSE_APPROVAL and r.allowed
    p = approval_payload(lv.lc.get(rec.setup_id), r)
    assert p.approval_allowed_now and p.approval_safety is r
    late = approval_check(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=(T0 + 630) * S)
    assert "candidate_expired" in late.hard_codes and not late.allowed
    assert not approval_payload(lv.lc.get(rec.setup_id), late).approval_allowed_now


def test_approval_check_rejects_non_actionable_status():
    lv, rec = started()
    lv.at(605)
    lv.sc.error(-1, 1100)
    lv.pump()
    rec2 = lv.lc.get(rec.setup_id)
    assert rec2.status is CandidateStatus.BLOCKED
    r = approval_check(lv.h.engine, rec2, now_wall_ns=(T0 + 606) * S)
    assert {"candidate_status_not_actionable", "connection_unusable"} <= r.hard_codes


def test_approval_check_hold_during_crossed_book():
    lv, rec = started()
    lv.at(605)
    lv.sc.depth(DEPTH, 0, 1, 1, (rec.candidate.entry_reference + 1) * TICK, 10)
    lv.pump()
    r = approval_check(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=(T0 + 605) * S)
    assert not r.allowed and not r.hard_block_reasons and r.temporary_hold_reasons


def test_creation_uses_the_same_policy(base):
    _, rec, _, _ = base
    c = rec.candidate
    assert c.safety.purpose == PURPOSE_CREATION and c.safety.allowed
    assert c.safety.schema_version == S_.SAFETY_SCHEMA_VERSION


# ============================================================================ approval payload gating

def test_actionable_payload_alone_is_never_approval_eligible():
    """Static candidate information without a fresh approval-time SafetyResult: never eligible."""
    from hermes.decision.approval import SAFETY_NOT_EVALUATED
    lv, rec = started()
    r = lv.lc.get(rec.setup_id)
    assert r.status is CandidateStatus.ACTIONABLE and r.approval_allowed_now      # lifecycle allows ...
    p = approval_payload(r)
    assert p.actionable and p.lifecycle_allows and not p.approval_allowed_now    # ... payload alone does not
    assert [d.code for d in p.approval_denied_reasons] == [SAFETY_NOT_EVALUATED]
    assert p.entry_reference == rec.candidate.entry_reference                   # static info still reported


def test_fresh_passing_safety_result_is_required_and_sufficient():
    from hermes.decision.approval import current_approval_payload
    lv, rec = started()
    r = lv.lc.get(rec.setup_id)
    now = (T0 + 605) * S
    p = current_approval_payload(lv.h.engine, r, now_wall_ns=now)
    assert p.approval_allowed_now and p.approval_denied_reasons == () and p.approval_safety.allowed


def test_forged_or_mismatched_safety_results_are_rejected(base):
    from hermes.decision.approval import (
        SAFETY_NOT_EVALUATED, SAFETY_RESULT_NOT_FOR_APPROVAL, SAFETY_RESULT_OTHER_CANDIDATE,
        SAFETY_RESULT_OUTDATED)
    lv, rec, _, f = base
    r = lv.lc.get(rec.setup_id)
    now = (T0 + 605) * S
    good = approval_check(lv.h.engine, r, now_wall_ns=now)
    assert good.allowed

    class Fake:                                                         # duck-typed "allowed=True"
        allowed = True
    codes = lambda s: {d.code for d in approval_payload(r, s).approval_denied_reasons}  # noqa: E731
    assert not approval_payload(r, Fake()).approval_allowed_now and SAFETY_NOT_EVALUATED in codes(Fake())
    lifecycle_result = ev(dataclasses.replace(f, now_wall_ns=now), purpose=PURPOSE_LIFECYCLE, baseline=r.candidate)
    assert lifecycle_result.allowed and SAFETY_RESULT_NOT_FOR_APPROVAL in codes(lifecycle_result)
    other = dataclasses.replace(good, subject="S" + "0" * 23)
    assert SAFETY_RESULT_OTHER_CANDIDATE in codes(other)
    old = dataclasses.replace(good, evaluated_wall_ns=r.status_wall_ns - 1)
    assert SAFETY_RESULT_OUTDATED in codes(old)
    for bad in (Fake(), lifecycle_result, other, old):
        assert not approval_payload(r, bad).approval_allowed_now


@pytest.mark.parametrize("final", ["BLOCKED", "EXPIRED", "STALE", "INVALIDATED", "NONE"])
def test_final_statuses_are_never_approval_eligible_even_with_a_passing_result(final):
    lv, rec = started()
    r = lv.lc.get(rec.setup_id)
    good = approval_check(lv.h.engine, r, now_wall_ns=(T0 + 605) * S)
    assert good.allowed
    done = dataclasses.replace(r, status=CandidateStatus(final), approval_allowed_now=False)
    p = approval_payload(done, good)
    assert not p.approval_allowed_now and "candidate_status_not_actionable" in {d.code for d in p.approval_denied_reasons}


def test_lifecycle_hold_denies_even_with_a_passing_result():
    lv, rec = started()
    r = lv.lc.get(rec.setup_id)
    good = approval_check(lv.h.engine, r, now_wall_ns=(T0 + 605) * S)
    held = dataclasses.replace(r, approval_allowed_now=False)
    p = approval_payload(held, good)
    assert not p.approval_allowed_now and "lifecycle_hold_active" in {d.code for d in p.approval_denied_reasons}


def test_failing_safety_result_reasons_are_reported_bare():
    lv, rec = started()
    lv.at(605)
    lv.sc.error(-1, 1100)
    lv.pump()
    r = lv.lc.get(rec.setup_id)
    p = approval_payload(r, approval_check(lv.h.engine, r, now_wall_ns=(T0 + 606) * S))
    codes = {d.code for d in p.approval_denied_reasons}
    assert not p.approval_allowed_now and {"connection_unusable", "candidate_status_not_actionable"} <= codes


def test_obsolete_entry_hours_key_is_an_explicit_error():
    with pytest.raises(ConfigError, match="entry_hours is obsolete: renamed to 'entry_policy'"):
        config_from_mapping({"decision": {"entry_hours": "RTH_ONLY"}})
