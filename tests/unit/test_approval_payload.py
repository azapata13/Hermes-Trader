"""C9e human-approval payload: one structured reason shape, content id, canonical form, rendering."""

from __future__ import annotations

import dataclasses
import json

import pytest

from hermes.decision.approval import (
    ApprovalPayload, SAFETY_NOT_EVALUATED, approval_payload, current_approval_payload, payload_to_dict,
    render_approval_text, verify_approval_view_id, verify_proposal_id)
from hermes.decision.candidate import Direction
from hermes.decision.lifecycle import CandidateStatus
from hermes.decision.reasons import SEVERITY_RANK, SOURCES, Reason, Severity, is_code, ordered
from tests.unit.test_candidate import PRE_RTH, S, T0, trend
from tests.unit.test_lifecycle import Live, started

NOW = (T0 + 605) * S


def _scenarios():
    yield Live(trend(+1, minutes=12))
    yield Live(trend(-1, minutes=12))
    yield Live(trend(+1, minutes=8, t0=PRE_RTH))


@pytest.fixture(scope="module")
def all_records():
    return [r for lv in _scenarios() for r in lv.lc.records]


# ============================================================================ Reason shape

def test_reason_shape_is_validated():
    Reason("safety", "book_not_valid", "suspect", Severity.BLOCK)
    with pytest.raises(ValueError):
        Reason("llm", "x", "", Severity.INFO)                        # unknown source
    with pytest.raises(ValueError):
        Reason("safety", "Book Not Valid", "", Severity.BLOCK)       # not a machine code
    with pytest.raises(ValueError):
        Reason("safety", "book_not_valid", "", "BLOCK")               # severity must be the enum
    assert is_code("5m_quality") and not is_code("_x") and not is_code("") and not is_code("a:b")


def test_ordered_is_by_severity_stable_and_deduplicated():
    a = Reason("orderflow", "ofi_supports", "", Severity.SUPPORT)
    b = Reason("safety", "book_not_valid", "", Severity.BLOCK)
    c = Reason("session", "mid_below_full_session_vwap", "", Severity.CAUTION)
    d = Reason("approval", "safety_not_evaluated", "", Severity.BLOCK)
    assert ordered([a, b, c, d, b]) == (b, d, c, a)


# ============================================================================ candidate reasons

def test_candidate_reasons_mirror_the_legacy_strings(all_records):
    """Every legacy string has exactly one structured twin (same bucket), codes are machine codes."""
    seen_dirs = set()
    for rec in all_records:
        c = rec.candidate
        seen_dirs.add(c.direction)
        for r in c.reasons:
            assert r.source in SOURCES and is_code(r.code)
        sev = [r.severity for r in c.reasons]
        assert sev.count(Severity.SUPPORT) == len(c.supporting_reasons)
        assert sev.count(Severity.CAUTION) == len(c.caution_reasons)
        assert sev.count(Severity.BLOCK) + sev.count(Severity.HOLD) == len(c.blocking_reasons)
        assert (c.direction is not Direction.NONE) == (not any(s in (Severity.BLOCK, Severity.HOLD) for s in sev))
    assert {Direction.LONG, Direction.SHORT, Direction.NONE} <= seen_dirs


def test_creation_safety_codes_are_bare_with_source_safety():
    lv = Live(trend(+1, minutes=8, t0=PRE_RTH))
    c = lv.lc.records[-1].candidate
    assert Reason("safety", "outside_authorized_entry_hours", c.safety.hard_block_reasons[0].detail,
                  Severity.BLOCK) in c.reasons
    assert all(not r.code.startswith(("safety:", "hold:")) for r in c.reasons)


# ============================================================================ payload

def test_payload_has_one_structured_reason_list():
    lv, rec = started()
    p = current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=NOW)
    for legacy in ("status_reasons", "supporting_reasons", "caution_reasons", "blocking_reasons",
                   "approval_denied_reasons", "temporary_hold_reasons"):
        assert legacy not in {f.name for f in dataclasses.fields(ApprovalPayload)}
    assert all(isinstance(r, Reason) for r in p.reasons)
    ranks = [SEVERITY_RANK[r.severity] for r in p.reasons]
    assert ranks == sorted(ranks)
    assert p.approval_allowed_now and p.denied_reasons == ()
    assert "trade_flow_supports" in p.codes(source="orderflow", severity=Severity.SUPPORT)
    assert "actionable_proposal" in p.codes(source="lifecycle", severity=Severity.INFO)
    assert p.units_per_point == 4 and p.safety_evaluated_wall_ns == NOW


def test_payload_without_safety_is_denied_with_structured_reason():
    lv, rec = started()
    p = approval_payload(lv.lc.get(rec.setup_id))
    assert not p.approval_allowed_now and p.safety_evaluated_wall_ns is None
    assert p.denied_reasons == (Reason("approval", SAFETY_NOT_EVALUATED, "no approval-time SafetyResult supplied",
                                       Severity.BLOCK),)
    assert p.reasons[0] == p.denied_reasons[0]                        # blocking evidence first


def test_lifecycle_reasons_after_creation():
    lv, rec = started()
    lv.tick_until(630)
    r = lv.lc.get(rec.setup_id)
    assert r.status is CandidateStatus.EXPIRED
    p = approval_payload(r)
    assert "expired" in p.codes(source="lifecycle", severity=Severity.BLOCK)
    assert "candidate_status_not_actionable" in p.codes(source="approval")
    assert "actionable_proposal" not in p.codes()


# ============================================================================ content id / canonical form

def test_ids_are_deterministic_and_verifiable():
    (lv1, r1), (lv2, r2) = started(), started()
    p1 = current_approval_payload(lv1.h.engine, lv1.lc.get(r1.setup_id), now_wall_ns=NOW)
    p2 = current_approval_payload(lv2.h.engine, lv2.lc.get(r2.setup_id), now_wall_ns=NOW)
    assert p1 == p2 and (p1.proposal_id, p1.approval_view_id) == (p2.proposal_id, p2.approval_view_id)
    assert p1.proposal_id.startswith("P") and len(p1.proposal_id) == 24
    assert p1.approval_view_id.startswith("V") and len(p1.approval_view_id) == 24
    assert verify_proposal_id(p1) and verify_approval_view_id(p1)


def test_same_proposal_later_safety_observation_keeps_proposal_id():
    lv, rec = started()
    r = lv.lc.get(rec.setup_id)
    p1 = current_approval_payload(lv.h.engine, r, now_wall_ns=NOW)
    p2 = current_approval_payload(lv.h.engine, r, now_wall_ns=NOW + 3 * S)      # time advanced only
    bare = approval_payload(r)                                                 # safety not evaluated
    assert p1.proposal_id == p2.proposal_id == bare.proposal_id
    assert len({p1.approval_view_id, p2.approval_view_id, bare.approval_view_id}) == 3
    lv.tick_until(630)                                                         # lifecycle status changes
    expired = approval_payload(lv.lc.get(rec.setup_id))
    assert expired.status == "EXPIRED" and expired.proposal_id == p1.proposal_id
    assert expired.approval_view_id != bare.approval_view_id
    annotated = lv.lc.annotate(rec.setup_id, "memory: similar setups (informational)")
    assert approval_payload(annotated).proposal_id == p1.proposal_id           # annotations are not the proposal


def test_hold_changes_the_view_not_the_proposal():
    from tests.support import DEPTH, TICK
    lv, rec = started()
    before = current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=NOW)
    lv.at(605)
    lv.sc.depth(DEPTH, 0, 1, 1, (rec.candidate.entry_reference + 1) * TICK, 10)  # crossed, still VALID
    lv.pump()
    held = current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=NOW)
    assert not held.approval_allowed_now and held.codes(severity=Severity.HOLD)
    assert held.proposal_id == before.proposal_id and held.approval_view_id != before.approval_view_id


@pytest.mark.parametrize("field,delta", [("entry_reference", 1), ("proposed_stop", -1),
                                         ("structural_invalidation", -1), ("risk_points", 0.25)])
def test_changed_proposal_facts_change_proposal_id(field, delta):
    lv, rec = started()
    p = current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=NOW)
    changed = dataclasses.replace(p, **{field: getattr(p, field) + delta})
    assert not verify_proposal_id(changed) and not verify_approval_view_id(changed)
    from hermes.decision.approval import compute_proposal_id
    assert compute_proposal_id(changed) != p.proposal_id
    cand = dataclasses.replace(rec.candidate, **{field: getattr(rec.candidate, field) + delta})
    rec2 = dataclasses.replace(lv.lc.get(rec.setup_id), candidate=cand)
    assert approval_payload(rec2).proposal_id != p.proposal_id           # built from a changed candidate


def test_canonical_form_is_plain_json_and_round_trips():
    lv, rec = started()
    p = current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=NOW)
    d = payload_to_dict(p)
    text = json.dumps(d, sort_keys=True)
    assert json.loads(text) == d
    assert d["proposal_id"] == p.proposal_id and d["approval_view_id"] == p.approval_view_id and d["mode"] == "HUMAN_APPROVAL"
    assert d["reasons"][0].keys() == {"source", "code", "detail", "severity"}
    assert "take_profit" not in text and "placeOrder" not in text


# ============================================================================ rendering

def test_render_is_deterministic_and_honest():
    (lv1, r1), (lv2, r2) = started(), started()
    p1 = current_approval_payload(lv1.h.engine, lv1.lc.get(r1.setup_id), now_wall_ns=NOW)
    p2 = current_approval_payload(lv2.h.engine, lv2.lc.get(r2.setup_id), now_wall_ns=NOW)
    t = render_approval_text(p1)
    assert t == render_approval_text(p2)
    assert "Hermès sends no order" in t and "Approval allowed now  : YES" in t
    assert "Take-profit           : NONE (by design)" in t
    assert f"{p1.entry_reference / 4:.2f}" in t and f"{p1.proposed_stop / 4:.2f}" in t
    assert "never redistributed" in t and "market-by-price" in t
    denied = render_approval_text(approval_payload(lv1.lc.get(r1.setup_id)))
    assert "Approval allowed now  : NO" in denied and "  approval/safety_not_evaluated" in denied


def test_render_none_and_blocked_records(all_records):
    for rec in all_records:
        t = render_approval_text(approval_payload(rec))
        assert "Approval allowed now  : NO" in t
        if rec.candidate.direction is Direction.NONE:
            assert "Entry reference" not in t and "BLOCKERS:\n  none" not in t


# ============================================================================ read-only

def test_building_payloads_changes_no_state():
    lv, rec = started()
    fp = lv.drv.fingerprint()
    from hermes.replay.fingerprint import state_hash
    h = state_hash(lv.h.engine)
    for _ in range(3):
        current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=NOW)
        render_approval_text(approval_payload(lv.lc.get(rec.setup_id)))
    assert lv.drv.fingerprint() == fp and state_hash(lv.h.engine) == h


# ============================================================================ replay determinism

class PayloadObserver:
    """Builds the approval-time payload the moment each ACTIONABLE candidate is registered."""

    def __init__(self, engine):
        from hermes.decision.candidate import CandidateEngine
        from hermes.decision.driver import CandidateDriver
        self.engine = engine
        self.drv = CandidateDriver(engine, CandidateEngine())
        self.out: list[tuple[str, str]] = []

    def after_event(self, raw, events, now):
        n = len(self.drv.lifecycle.records)
        self.drv.after_event(raw, events, now)
        for rec in list(self.drv.lifecycle.records)[n:]:
            if rec.status is CandidateStatus.ACTIONABLE:
                p = current_approval_payload(self.engine, rec, now_wall_ns=raw.recv_wall_ns)
                self.out.append((p.proposal_id, p.approval_view_id, render_approval_text(p)))


def test_payloads_are_identical_live_and_in_replay(tmp_path):
    from hermes.replay.runner import ReplayOptions, replay_session
    from tests.support import write_hrec
    sc = trend(+1, minutes=12)
    d = write_hrec(tmp_path / "s", sc.events)
    opts = ReplayOptions(observers=(PayloadObserver,))
    a, b = replay_session(d, opts), replay_session(d, opts)
    from tests.support import Harness
    h = Harness()
    obs = PayloadObserver(h.engine)
    for raw in sc.events:
        h.feed([raw])
        obs.after_event(raw, (), 0)
    assert a.observers[0].out == b.observers[0].out == obs.out and obs.out
    assert all(pid.startswith("P") and vid.startswith("V") for pid, vid, _ in obs.out)
    assert a.final_hash == replay_session(d).final_hash


def test_render_separates_severity_sections_and_shows_ids_and_expiry():
    from tests.support import DEPTH, TICK
    lv, rec = started()
    p = current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=NOW)
    t = render_approval_text(p)
    for line in (f"setup_id              : {p.setup_id}", f"proposal_id           : {p.proposal_id}",
                 f"approval_view_id      : {p.approval_view_id}", "Lifecycle status      : ACTIONABLE",
                 "(25 s remaining at view time)", "BLOCKERS:\n  none", "TEMPORARY HOLDS:\n  none",
                 "SUPPORTING EVIDENCE:", "INFO:\n  lifecycle/actionable_proposal"):
        assert line in t, line
    assert t.index("BLOCKERS:") < t.index("TEMPORARY HOLDS:") < t.index("SUPPORTING EVIDENCE:") < t.index("INFO:")
    assert "confidence" not in t.lower() and "score" not in t.lower()     # no artificial global score
    lv.at(605)
    lv.sc.depth(DEPTH, 0, 1, 1, (rec.candidate.entry_reference + 1) * TICK, 10)
    lv.pump()
    held = render_approval_text(current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=NOW))
    assert "TEMPORARY HOLDS:\n  approval/lifecycle_hold_active" in held and "crossed_book_transition" in held
    assert "Approval allowed now  : NO" in held


def test_legacy_strings_are_never_parsed_for_decisions(all_records):
    """Scrambling/emptying the deprecated strings changes no status or actionability decision."""
    from hermes.config import DecisionConfig
    from hermes.decision.lifecycle import initial_record
    cfg = DecisionConfig()
    for rec in all_records:
        c = rec.candidate
        for legacy in ((), ("garbage:not_a_code",), ("trigger_evaluation_stale:1ms>0ms", "safety:book_not_valid:x")):
            c2 = dataclasses.replace(c, blocking_reasons=legacy, supporting_reasons=legacy, caution_reasons=legacy)
            assert c2.is_actionable_proposal == c.is_actionable_proposal
            assert initial_record(c2, cfg).status is initial_record(c, cfg).status
