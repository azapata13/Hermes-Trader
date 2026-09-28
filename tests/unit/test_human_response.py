"""C9e pure HumanApprovalResponse: intent only, never an execution authorization, never bypasses safety."""

from __future__ import annotations

import dataclasses

import pytest

from hermes.decision.approval import approval_payload, current_approval_payload
from hermes.decision.lifecycle import CandidateStatus
from hermes.decision.response import (
    HumanAction, HumanApprovalResponse, execution_prerequisites, response_matches_view)
from hermes.decision.safety import approval_check
from hermes.replay.fingerprint import state_hash
from tests.unit.test_candidate import S, T0
from tests.unit.test_lifecycle import started

NOW = (T0 + 605) * S
ALWAYS = {"risk_eligibility_policy_not_implemented", "execution_layer_disabled"}


def enter(view, action=HumanAction.ENTER, **kw):
    return HumanApprovalResponse(action, view.setup_id, view.proposal_id, view.approval_view_id, **kw)


def test_response_is_immutable_and_validated():
    lv, rec = started()
    v = current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=NOW)
    r = enter(v, response_wall_ns=NOW + S, response_seq=10, note="looks clean")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.action = HumanAction.REJECT  # type: ignore[misc]
    with pytest.raises(ValueError):
        HumanApprovalResponse("ENTER", v.setup_id, v.proposal_id, v.approval_view_id)   # type: ignore[arg-type]
    with pytest.raises(ValueError):
        HumanApprovalResponse(HumanAction.ENTER, v.setup_id, v.approval_view_id, v.approval_view_id)  # wrong kind
    with pytest.raises(ValueError):
        enter(v, note="x" * 501)
    with pytest.raises(ValueError):
        enter(v, response_wall_ns=-1)


def test_enter_is_never_an_execution_authorization():
    lv, rec = started()
    v = current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=NOW)
    assert v.approval_allowed_now
    r = enter(v, response_wall_ns=NOW)
    assert not r.authorizes_execution
    ok, why = response_matches_view(r, v)
    assert ok and why == ()
    fresh = approval_check(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=NOW)
    pre = execution_prerequisites(r, lv.lc.get(rec.setup_id), fresh)
    assert not pre.satisfied and set(pre.codes) == ALWAYS                # everything else OK; still never
    assert not execution_prerequisites(enter(v, HumanAction.REJECT), lv.lc.get(rec.setup_id), fresh).satisfied


def test_response_creation_changes_no_state():
    lv, rec = started()
    fp, h = lv.drv.fingerprint(), state_hash(lv.h.engine)
    before = lv.lc.get(rec.setup_id)
    v = current_approval_payload(lv.h.engine, before, now_wall_ns=NOW)
    r = enter(v, response_wall_ns=NOW)
    execution_prerequisites(r, before, approval_check(lv.h.engine, before, now_wall_ns=NOW))
    response_matches_view(r, v)
    assert lv.lc.get(rec.setup_id) is before and lv.drv.fingerprint() == fp and state_hash(lv.h.engine) == h


def test_old_safe_view_never_bypasses_fresh_safety():
    """ENTER on a view that WAS approvable; safety later fails -> prerequisites carry the bare codes."""
    lv, rec = started()
    v = current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=NOW)
    assert v.approval_allowed_now
    r = enter(v, response_wall_ns=NOW + S)
    lv.at(606)
    lv.sc.error(-1, 1100)                                                # connection lost after the view
    lv.pump()
    rec2 = lv.lc.get(rec.setup_id)
    fresh = approval_check(lv.h.engine, rec2, now_wall_ns=NOW + 2 * S)
    pre = execution_prerequisites(r, rec2, fresh)
    assert {"connection_unusable", "proposal_not_valid"} <= set(pre.codes) and not pre.satisfied
    assert rec2.status is CandidateStatus.BLOCKED                         # the response changed nothing


def test_stale_or_missing_or_forged_safety_is_rejected():
    lv, rec = started()
    r0 = lv.lc.get(rec.setup_id)
    v = current_approval_payload(lv.h.engine, r0, now_wall_ns=NOW)
    r = enter(v, response_wall_ns=NOW + 2 * S)
    assert "fresh_safety_missing" in execution_prerequisites(r, r0, None).codes
    older = approval_check(lv.h.engine, r0, now_wall_ns=NOW)            # evaluated BEFORE the response
    assert "fresh_safety_outdated" in execution_prerequisites(r, r0, older).codes
    forged = dataclasses.replace(older, subject="S" + "0" * 23, evaluated_wall_ns=NOW + 3 * S)
    assert "fresh_safety_mismatch" in execution_prerequisites(r, r0, forged).codes

    class Fake:
        allowed = True
    assert "fresh_safety_missing" in execution_prerequisites(r, r0, Fake()).codes  # type: ignore[arg-type]


def test_mismatched_or_tampered_views_are_reported():
    lv, rec = started()
    r0 = lv.lc.get(rec.setup_id)
    v = current_approval_payload(lv.h.engine, r0, now_wall_ns=NOW)
    later = current_approval_payload(lv.h.engine, r0, now_wall_ns=NOW + S)
    r = enter(v)
    ok, why = response_matches_view(r, later)                           # same proposal, other view
    assert not ok and [x.code for x in why] == ["response_view_mismatch"]
    tampered = dataclasses.replace(v, proposed_stop=v.proposed_stop - 8)
    assert "approval_view_id_invalid" in {x.code for x in response_matches_view(r, tampered)[1]}
    bare = approval_payload(r0)                                          # not approval-eligible view
    ok, why = response_matches_view(enter(bare), bare)
    assert not ok and "enter_on_non_approvable_view" in {x.code for x in why}


def test_changed_proposal_is_detected():
    lv, rec = started()
    r0 = lv.lc.get(rec.setup_id)
    v = current_approval_payload(lv.h.engine, r0, now_wall_ns=NOW)
    r = enter(v, response_wall_ns=NOW)
    moved = dataclasses.replace(r0, candidate=dataclasses.replace(r0.candidate, proposed_stop=r0.candidate.proposed_stop - 4))
    pre = execution_prerequisites(r, moved, approval_check(lv.h.engine, moved, now_wall_ns=NOW))
    assert "proposal_changed" in pre.codes and not pre.satisfied
