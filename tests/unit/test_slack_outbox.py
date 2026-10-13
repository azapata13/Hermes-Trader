"""Slack outbox hardening (live symptom: ENTER / REJECT appearing and disappearing about once per second).

Root cause reproduced here: a VALID book is momentarily crossed many times per minute. Each moment
is a correct TEMPORARY_HOLD for the decision core, and each HOLD_ENTERED / HOLD_CLEARED pair used to
redraw the Slack message (buttons removed, then restored), one HTTP update at a time.
Display only: ENTER is always re-checked against the live state when clicked.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from hermes.config import DecisionConfig
from hermes.decision.approval import current_approval_payload
from hermes.decision.runtime import JournalKind
from hermes.slack.bridge import HOLD_DISPLAY_DELAY_S, SlackApprovalBridge
from hermes.slack.fake import FakeSlackTransport
from hermes.slack.journal import ApprovalJournal
from hermes.slack.protocol import ENTER_ACTION_ID, REJECT_ACTION_ID, SlackAction, SlackInteraction
from tests.support import DEPTH, TICK
from tests.unit.test_lifecycle import started
from tests.unit.test_slack_approval import NOW, _action_ids, _action_value, _process_click, _record, _wait


def _bridge(lv, fake=None, **kw):
    fake = fake or FakeSlackTransport()
    b = SlackApprovalBridge(SimpleNamespace(driver=lv.drv), lv.h.engine, DecisionConfig(), fake, ApprovalJournal(),
                            **kw)
    b.start()
    return b, fake


def _views(lv, rec):
    """(initial, held, cleared) payloads of the same proposal, produced by real book changes."""
    initial = current_approval_payload(lv.h.engine, rec, now_wall_ns=NOW)
    lv.at(606)
    lv.sc.depth(DEPTH, 0, 1, 1, (rec.candidate.entry_reference + 1) * TICK, 10)      # crossed: hold
    lv.pump()
    held = current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=lv.sc.wall)
    lv.sc.depth(DEPTH, 0, 1, 1, (rec.candidate.entry_reference - 1) * TICK, 10)      # coherent again
    lv.pump()
    cleared = current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=lv.sc.wall)
    assert held.status == "ACTIONABLE" and not held.approval_allowed_now
    assert cleared.approval_allowed_now and cleared.proposal_id == held.proposal_id == initial.proposal_id
    return initial, held, cleared


def _post(lv, b, fake, p):
    b.publish_decision(_record(JournalKind.APPROVAL_VIEW_CREATED, p, lv), p)
    _wait(lambda: len(fake.posts) == 1)


def _flap(lv, b, held, cleared, n):
    for _ in range(n):
        b.publish_decision(_record(JournalKind.TEMPORARY_HOLD_ENTERED, held, lv), held)
        b.publish_decision(_record(JournalKind.TEMPORARY_HOLD_CLEARED, cleared, lv), cleared)


def test_production_default_debounces_holds():
    assert HOLD_DISPLAY_DELAY_S >= 0.5


def test_transient_hold_flapping_never_redraws_the_message():
    lv, rec = started()
    b, fake = _bridge(lv, hold_display_delay_s=5.0)
    try:
        initial, held, cleared = _views(lv, rec)
        _post(lv, b, fake, initial)
        _flap(lv, b, held, cleared, 25)                    # ~25 s of a quiet overnight book
        b._outbox.join()
        assert fake.updates == []                          # ENTER / REJECT never disappeared
        assert ENTER_ACTION_ID in _action_ids(fake.posts[0][1])
        assert b.stats.holds_suppressed + b.stats.coalesced >= 25
        assert b.displayed_view(initial.proposal_id).approval_view_id == initial.approval_view_id
    finally:
        b.close()


def test_a_hold_that_persists_is_drawn_after_the_delay_and_cleared_after():
    lv, rec = started()
    b, fake = _bridge(lv, hold_display_delay_s=0.2)
    try:
        initial, held, cleared = _views(lv, rec)
        _post(lv, b, fake, initial)
        t0 = time.monotonic()
        b.publish_decision(_record(JournalKind.TEMPORARY_HOLD_ENTERED, held, lv), held)
        _wait(lambda: len(fake.updates) == 1)
        assert time.monotonic() - t0 >= 0.18
        assert ENTER_ACTION_ID not in _action_ids(fake.updates[-1][1])
        assert REJECT_ACTION_ID in _action_ids(fake.updates[-1][1])
        b.publish_decision(_record(JournalKind.TEMPORARY_HOLD_CLEARED, cleared, lv), cleared)
        _wait(lambda: len(fake.updates) == 2)
        assert ENTER_ACTION_ID in _action_ids(fake.updates[-1][1])
    finally:
        b.close()


def test_a_burst_coalesces_to_the_latest_state_and_closing_always_wins():
    lv, rec = started()
    fake = FakeSlackTransport(latency_s=0.3)               # the worker is busy posting meanwhile
    b, fake = _bridge(lv, fake, hold_display_delay_s=0.0)
    try:
        initial, held, cleared = _views(lv, rec)
        b.publish_decision(_record(JournalKind.APPROVAL_VIEW_CREATED, initial, lv), initial)
        time.sleep(0.05)
        _flap(lv, b, held, cleared, 10)
        b.publish_decision(_record(JournalKind.TEMPORARY_HOLD_ENTERED, held, lv), held)
        lv.tick_until(630)                                 # past the TTL: EXPIRED
        closed = current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=lv.sc.wall)
        assert closed.status == "EXPIRED"
        b.publish_decision(_record(JournalKind.CANDIDATE_EXPIRED, closed, lv), closed)
        b._outbox.join()
        assert len(fake.posts) == 1 and len(fake.updates) <= 1
        assert b.stats.coalesced >= 20
        assert _action_ids(fake.updates[-1][1]) == []      # final state: closed, no button
    finally:
        b.close()


def test_enter_after_suppressed_holds_is_still_evaluated_against_the_live_state():
    lv, rec = started()
    b, fake = _bridge(lv, hold_display_delay_s=5.0)
    try:
        initial, held, cleared = _views(lv, rec)
        _post(lv, b, fake, initial)
        _flap(lv, b, held, cleared, 5)
        b._outbox.join()
        v = _action_value(fake.posts[0][1], ENTER_ACTION_ID)          # the buttons the trader still sees
        click = SlackInteraction(interaction_id="i-1", user_id="U123", action=SlackAction.ENTER,
                                 setup_id=v["s"], proposal_id=v["p"], approval_view_id=v["v"],
                                 interaction_wall_ns=NOW)
        fake.emit(click)
        _process_click(lv, b, click, at=607)
        _wait(lambda: len(b.journal.entries) == 1)
        audit = b.journal.entries[0]
        assert audit.accepted_human_intent and not audit.authorizes_execution
        assert "slack_view_stale" not in audit.result_codes
        assert "execution_layer_disabled" in audit.result_codes
        _wait(lambda: len(fake.updates) >= 1)
        assert "No order was sent" in json.dumps(fake.updates[-1][1].blocks)
    finally:
        b.close()


def test_enter_during_a_live_hold_fails_closed_and_redraws_immediately():
    lv, rec = started()
    b, fake = _bridge(lv, hold_display_delay_s=5.0)
    try:
        initial = current_approval_payload(lv.h.engine, rec, now_wall_ns=NOW)
        _post(lv, b, fake, initial)
        lv.at(606)
        lv.sc.depth(DEPTH, 0, 1, 1, (rec.candidate.entry_reference + 1) * TICK, 10)  # hold still in effect
        lv.pump()
        v = _action_value(fake.posts[0][1], ENTER_ACTION_ID)
        click = SlackInteraction(interaction_id="i-2", user_id="U123", action=SlackAction.ENTER,
                                 setup_id=v["s"], proposal_id=v["p"], approval_view_id=v["v"])
        fake.emit(click)
        _process_click(lv, b, click, at=607)
        _wait(lambda: len(b.journal.entries) == 1)
        audit = b.journal.entries[0]
        assert not audit.accepted_human_intent and "enter_failed_closed" in audit.result_codes
        _wait(lambda: len(fake.updates) == 1, timeout=1.0)            # not delayed by the debounce
        assert ENTER_ACTION_ID not in _action_ids(fake.updates[-1][1])
    finally:
        b.close()


def test_screenshot_is_requested_for_an_actionable_proposal_posted_during_a_transient_hold(monkeypatch):
    monkeypatch.setenv("HERMES_SCREENSHOT_ENABLED", "1")
    lv, rec = started()
    b, fake = _bridge(lv, hold_display_delay_s=0.0)
    submitted = []
    b.enrichment.submit = lambda p, ref: submitted.append((p.proposal_id, ref))
    try:
        _, held, _ = _views(lv, rec)
        b.publish_decision(_record(JournalKind.APPROVAL_VIEW_CREATED, held, lv), held)
        _wait(lambda: len(fake.posts) == 1)
        assert submitted == [(held.proposal_id, fake.posts[0][0])]
    finally:
        b.close()


def test_closed_decision_view_never_overwrites_the_setup_tracking_view():
    from tests.unit.test_slack_pro import _rearm_run

    r, old, _ = _rearm_run()
    lc = r.rt.driver.lifecycle
    fake = FakeSlackTransport()
    b = SlackApprovalBridge(r.rt, r.h.engine, r.rt.cfg, fake, ApprovalJournal())
    b.start()
    try:
        rec = lc.get(old.setup_id)
        lv = SimpleNamespace(sc=r.sc)
        p0 = current_approval_payload(r.h.engine, rec, now_wall_ns=rec.created_wall_ns)
        b.publish_decision(_record(JournalKind.APPROVAL_VIEW_CREATED, p0, lv), p0)
        _wait(lambda: len(fake.posts) == 1)
        # live order: the setup event is published BEFORE the CANDIDATE_* record of the same raw event
        b.publish_setup(*next((e, v) for e, v in r.events if e.kind == "WINDOW_CLOSED" and e.setup_id == old.setup_id))
        closed = current_approval_payload(r.h.engine, rec, now_wall_ns=r.sc.wall)
        assert closed.status != "ACTIONABLE"
        b.publish_decision(_record(JournalKind.CANDIDATE_EXPIRED, closed, lv), closed)
        b._outbox.join()
        assert b.stats.superseded == 1
        assert "SETUP MONITORING" in json.dumps(fake.updates[-1][1].blocks, ensure_ascii=False)
        assert _action_ids(fake.updates[-1][1]) == []
    finally:
        b.close()


def test_screenshot_is_taken_once_and_kept_through_every_later_update(monkeypatch):
    """The TWS screenshot is a thread reply on the proposal message: later updates (holds, closing,
    coalesced bursts) edit the message itself and can never remove it, re-take it or attach it elsewhere."""
    monkeypatch.setenv("HERMES_SCREENSHOT_ENABLED", "1")
    lv, rec = started()
    b, fake = _bridge(lv, hold_display_delay_s=0.0)
    submitted = []
    b.enrichment.submit = lambda p, ref: submitted.append((p.proposal_id, ref))
    try:
        initial, held, cleared = _views(lv, rec)
        _post(lv, b, fake, initial)
        ref = fake.posts[0][0]
        _flap(lv, b, held, cleared, 3)
        lv.tick_until(630)
        closed = current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=lv.sc.wall)
        b.publish_decision(_record(JournalKind.CANDIDATE_EXPIRED, closed, lv), closed)
        b._outbox.join()
        assert submitted == [(initial.proposal_id, ref)]               # once, on the right message
        assert len(fake.posts) == 1 and fake.updates and all(u[0] == ref for u in fake.updates)
        assert fake.uploads == []                                       # updates never upload / replace files
    finally:
        b.close()
