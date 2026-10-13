"""V1 Pro Slack: monitoring / re-arm / end messages (never an entry), History from recorded shadow
setups only (>= 30), re-arm note on the fresh proposal, D1.9 message otherwise unchanged."""

from __future__ import annotations

import json

from hermes.decision.approval import current_approval_payload
from hermes.decision.lifecycle import CandidateStatus
from hermes.decision.runtime import JournalKind
from hermes.slack.bridge import SlackApprovalBridge
from hermes.slack.fake import FakeSlackTransport
from hermes.slack.journal import ApprovalJournal
from hermes.slack.pro import history_for, rearm_note
from hermes.slack.render import render_setup_message, render_text
from tests.unit.test_analytics import row as srow
from tests.unit.test_candidate import trend
from tests.unit.test_setup_tracking import RT
from tests.unit.test_slack_approval import _action_ids, _record, _wait


def _rearm_run():
    r = RT(trend(+1, minutes=10.55))                       # 601 ACTIONABLE, 630 EXPIRED, 631 re-armed
    lc = r.rt.driver.lifecycle
    first = next(x for x in lc.records if x.status is not CandidateStatus.NONE)
    old = r.tracker.get(first.setup_id)
    new = r.tracker.get(old.rearmed_by)
    return r, old, new


# ---------------------------------------------------------------------------- rendering

def test_monitoring_message_has_no_entry_and_says_a_fresh_trigger_is_required():
    r, old, _new = _rearm_run()
    closed = next(v for e, v in r.events if e.kind == "WINDOW_CLOSED" and e.setup_id == old.setup_id)
    msg = render_setup_message(closed, "WINDOW_CLOSED")
    text = render_text(msg)
    assert _action_ids(msg) == [] and not any(b["type"] == "actions" for b in msg.blocks)
    assert "SETUP MONITORING" in text and "Old entry NOT available" in text
    assert "fresh 30 s trigger" in text and "no order sent" in text.lower()
    assert f"Original entry {old.original_entry_units / 4:.2f}" in text and "(EXPIRED)" in text


def test_rearmed_message_points_to_the_new_entry_and_carries_no_entry_itself():
    r, old, new = _rearm_run()
    rearmed = next(v for e, v in r.events if e.kind == "REARMED" and e.setup_id == old.setup_id)
    text = render_text(render_setup_message(rearmed, "REARMED"))
    assert "SETUP RE-ARMED" in text and f"{new.original_entry_units / 4:.2f}" in text
    assert "This message has no entry" in text


def test_final_message_reports_shadow_outcome_without_profit_language():
    r, old, _ = _rearm_run()
    r.rt.finalize()
    final = r.tracker.get(old.setup_id)
    text = render_text(render_setup_message(final, "OUTCOME_FINAL", human="REJECT recorded"))
    assert "SETUP ENDED" in text and "+1R" in text and "Human: REJECT recorded" in text
    # V1 Pro: the SHADOW RESULT block, always labelled; "P&L" only appears inside "NOT REAL P&L"
    assert "⚪ SETUP NOT TAKEN — rejected" in text and "SHADOW RESULT" in text and "+10: NO" in text
    assert "SHADOW ONLY — NOT REAL P&amp;L" in text                     # Slack mrkdwn escapes "&"
    low = text.lower().replace("not real p&amp;l", "")
    assert "profit" not in low and "p&amp;l" not in low and "p&l" not in low


# ---------------------------------------------------------------------------- providers

def test_rearm_note_only_for_rearmed_candidates():
    r, old, new = _rearm_run()
    p_new = type("P", (), {"setup_id": new.setup_id})()
    p_old = type("P", (), {"setup_id": old.setup_id})()
    note = rearm_note(r.tracker, p_new)
    assert note.startswith("↻ Re-arm of an earlier LONG setup") and f"{old.original_entry_units / 4:.2f}" in note
    assert rearm_note(r.tracker, p_old) is None and rearm_note(None, p_new) is None


def test_history_provider_needs_30_recorded_comparables_and_labels_shadow():
    r, old, _ = _rearm_run()
    feats = dict(old.features)
    p = type("P", (), {"setup_id": old.setup_id})()
    rows29 = [srow(f"S{i}", features=feats) for i in range(29)]
    assert history_for(r.tracker, lambda: rows29, p) is None
    rows30 = rows29 + [srow("S29", features=feats, r1=False)]
    h = history_for(r.tracker, lambda: rows30, p)
    assert h is not None and h.samples == 30 and h.wins == 29 and h.shadow and h.outcome_label == "+1R"
    from hermes.slack.summary import history_line
    assert history_line(h) == "History: 97% +1R · 30 similar setups (shadow)"


# ---------------------------------------------------------------------------- bridge integration

def _bridge(rt_obj, **kw):
    fake = FakeSlackTransport()
    b = SlackApprovalBridge(rt_obj.rt, rt_obj.h.engine, rt_obj.rt.cfg, fake, ApprovalJournal(), **kw)
    b.start()
    return b, fake


def _post(rt_obj, b, fake, rec):
    p = current_approval_payload(rt_obj.h.engine, rec, now_wall_ns=rec.created_wall_ns)
    lv = type("LV", (), {"sc": rt_obj.sc})()
    b.publish_decision(_record(JournalKind.APPROVAL_VIEW_CREATED, p, lv), p)
    _wait(lambda: b.displayed_view(p.proposal_id) is not None)
    return p


def test_bridge_updates_the_existing_message_with_the_monitoring_view_and_never_posts_a_new_one():
    r, old, _ = _rearm_run()
    lc = r.rt.driver.lifecycle
    b, fake = _bridge(r)
    try:
        p = _post(r, b, fake, lc.get(old.setup_id))
        closed = next((e, v) for e, v in r.events if e.kind == "WINDOW_CLOSED" and e.setup_id == old.setup_id)
        b.publish_setup(*closed)
        _wait(lambda: b.stats.setup_updates == 1)
        assert len(fake.posts) == 1 and fake.updates[-1][0] == b.message_ref(p.proposal_id)
        assert _action_ids(fake.updates[-1][1]) == []
        assert "SETUP MONITORING" in json.dumps(fake.updates[-1][1].blocks, ensure_ascii=False)
    finally:
        b.close()


def test_setup_event_for_a_proposal_without_message_is_dropped():
    r, _old, _ = _rearm_run()
    b, fake = _bridge(r)
    try:
        ev = next((e, v) for e, v in r.events if e.kind == "REARMED")
        b.publish_setup(*ev)
        import time
        time.sleep(0.05)
        assert fake.posts == [] and fake.updates == [] and b.stats.setup_updates == 0
    finally:
        b.close()


def test_fresh_rearmed_proposal_shows_the_rearm_note_and_history_only_when_backed():
    r, _old, new = _rearm_run()
    lc = r.rt.driver.lifecycle
    feats = dict(new.features)
    rows = [srow(f"H{i}", features=feats) for i in range(31)]
    b, fake = _bridge(r, note_provider=lambda p: rearm_note(r.tracker, p),
                      history_provider=lambda p: history_for(r.tracker, lambda: rows, p))
    try:
        _post(r, b, fake, lc.get(new.setup_id))
        text = json.dumps(fake.posts[0][1].blocks, ensure_ascii=False)
        assert "Re-arm of an earlier LONG setup" in text
        assert "History: 100% +1R · 31 similar setups (shadow)" in text
        assert "ENTER" in text and "Intent only — no order sent" in text          # D1.9 contract intact
    finally:
        b.close()
    b2, fake2 = _bridge(r, history_provider=lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        _post(r, b2, fake2, lc.get(new.setup_id))
        assert "History" not in json.dumps(fake2.posts[0][1].blocks)               # provider failure: no line
    finally:
        b2.close()


def test_monitoring_update_after_reject_keeps_the_human_decision_visible():
    r, old, _ = _rearm_run()
    lc = r.rt.driver.lifecycle
    b, fake = _bridge(r)
    try:
        p = _post(r, b, fake, lc.get(old.setup_id))
        b._human_actions[p.proposal_id] = "REJECT recorded"          # as set by an accepted REJECT click
        closed = next((e, x) for e, x in r.events if e.kind == "WINDOW_CLOSED" and e.setup_id == old.setup_id)
        b.publish_setup(*closed)
        _wait(lambda: b.stats.setup_updates == 1)
        assert "Human: REJECT recorded" in json.dumps(fake.updates[-1][1].blocks)
    finally:
        b.close()
