"""D1 Slack HUMAN_APPROVAL: deterministic rendering, async transport and fail-closed clicks."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from hermes.config import DecisionConfig
from hermes.decision.approval import current_approval_payload
from hermes.decision.runtime import JournalKind, JournalRecord
from hermes.replay.fingerprint import state_hash
from hermes.slack.bridge import SlackApprovalBridge
from hermes.slack.fake import FakeSlackTransport
from hermes.slack.journal import ApprovalJournal
from hermes.slack.protocol import (
    ENTER_ACTION_ID,
    REJECT_ACTION_ID,
    SlackAction,
    SlackInteraction,
)
from hermes.slack.render import render_slack_message, render_text
from hermes.slack.socket_mode import SlackConfigError, SlackSettings
from tests.support import DEPTH, TICK
from tests.unit.test_candidate import S, T0
from tests.unit.test_lifecycle import started

NOW = (T0 + 605) * S


def _wait(pred, timeout=1.5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.005)
    assert pred(), "timed out waiting for Slack worker"


def _action_ids(message):
    return [
        e["action_id"]
        for b in message.blocks
        if b.get("type") == "actions"
        for e in b.get("elements", ())
    ]


def _action_value(message, action_id):
    for b in message.blocks:
        for e in b.get("elements", ()):
            if e.get("action_id") == action_id:
                return json.loads(e["value"])
    raise AssertionError(f"missing {action_id}")


def _record(kind, p, lv):
    return JournalRecord(
        kind=kind,
        seq=lv.sc.seq,
        wall_ns=p.safety_evaluated_wall_ns or lv.sc.wall,
        instrument_id=p.instrument_id,
        setup_id=p.setup_id,
        proposal_id=p.proposal_id,
        approval_view_id=p.approval_view_id,
        status=p.status,
        direction=p.direction,
        approval_allowed_now=p.approval_allowed_now,
        reasons=(),
        decision_fingerprint="f" * 64,
    )


def _bridge(lv, fake=None):
    fake = fake or FakeSlackTransport()
    runtime = SimpleNamespace(driver=lv.drv)
    bridge = SlackApprovalBridge(runtime, lv.h.engine, DecisionConfig(), fake, ApprovalJournal())
    bridge.start()
    return bridge, fake


def _post_initial(lv, bridge, fake):
    rec = lv.lc.latest()
    p = current_approval_payload(lv.h.engine, rec, now_wall_ns=NOW)
    bridge.publish_decision(_record(JournalKind.APPROVAL_VIEW_CREATED, p, lv), p)
    _wait(lambda: len(fake.posts) == 1)
    _wait(lambda: bridge.displayed_view(p.proposal_id) is not None)
    return rec, p


def _process_click(lv, bridge, interaction, at=606):
    lv.at(at)
    raw = lv.sc.tick()
    lv.h.feed([raw])
    lv.drv.after_event(raw, (), raw.recv_mono_ns)
    bridge.after_event(raw, (), raw.recv_mono_ns)


def test_slack_settings_are_optional_but_partial_config_fails_closed():
    assert SlackSettings.from_env({}) is None
    env = {
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_APP_TOKEN": "xapp-test",
        "HERMES_SLACK_CHANNEL_ID": "C123",
    }
    assert SlackSettings.from_env(env).channel_id == "C123"
    try:
        SlackSettings.from_env({"SLACK_BOT_TOKEN": "xoxb-test"})
    except SlackConfigError as exc:
        assert "partial Slack configuration" in str(exc)
    else:
        raise AssertionError("partial Slack config was silently accepted")


def test_render_actionable_is_concise_and_buttons_carry_compact_ids():
    lv, rec = started()
    p = current_approval_payload(lv.h.engine, rec, now_wall_ns=NOW)
    msg = render_slack_message(p)
    assert _action_ids(msg) == [ENTER_ACTION_ID, REJECT_ACTION_ID]
    value = _action_value(msg, ENTER_ACTION_ID)
    assert value == {"s": p.setup_id, "p": p.proposal_id, "v": p.approval_view_id}
    visible = render_text(msg)                                   # what the trader reads (not button values)
    assert "Intent only — no order sent" in visible
    for hidden in (p.setup_id, p.proposal_id, p.approval_view_id, "Take-profit", "market-by-price", "MBP"):
        assert hidden not in visible
    assert "confidence" not in json.dumps(msg.blocks, ensure_ascii=False).lower()


def test_temporary_hold_removes_enter_and_same_message_is_updated():
    lv, rec = started()
    bridge, fake = _bridge(lv)
    try:
        _, initial = _post_initial(lv, bridge, fake)
        ref0 = fake.posts[0][0]

        lv.at(606)
        lv.sc.depth(DEPTH, 0, 1, 1, (rec.candidate.entry_reference + 1) * TICK, 10)
        lv.pump()
        held_rec = lv.lc.get(rec.setup_id)
        held = current_approval_payload(lv.h.engine, held_rec, now_wall_ns=lv.sc.wall)
        assert held.status == "ACTIONABLE" and not held.approval_allowed_now
        bridge.publish_decision(_record(JournalKind.TEMPORARY_HOLD_ENTERED, held, lv), held)
        _wait(lambda: len(fake.updates) == 1)
        assert fake.updates[-1][0] == ref0
        assert ENTER_ACTION_ID not in _action_ids(fake.updates[-1][1])
        assert REJECT_ACTION_ID in _action_ids(fake.updates[-1][1])

        lv.sc.depth(DEPTH, 0, 1, 1, (rec.candidate.entry_reference - 1) * TICK, 10)
        lv.pump()
        clear_rec = lv.lc.get(rec.setup_id)
        clear = current_approval_payload(lv.h.engine, clear_rec, now_wall_ns=lv.sc.wall)
        bridge.publish_decision(_record(JournalKind.TEMPORARY_HOLD_CLEARED, clear, lv), clear)
        _wait(lambda: len(fake.updates) == 2)
        assert fake.updates[-1][0] == ref0
        assert ENTER_ACTION_ID in _action_ids(fake.updates[-1][1])
        assert clear.proposal_id == initial.proposal_id
    finally:
        bridge.close()


def test_expired_candidate_disables_all_buttons():
    lv, rec = started()
    bridge, fake = _bridge(lv)
    try:
        _post_initial(lv, bridge, fake)
        lv.tick_until(630)
        expired = current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=lv.sc.wall)
        bridge.publish_decision(_record(JournalKind.CANDIDATE_EXPIRED, expired, lv), expired)
        _wait(lambda: len(fake.updates) == 1)
        assert _action_ids(fake.updates[-1][1]) == []
        assert "EXPIRED" in json.dumps(fake.updates[-1][1].blocks)
    finally:
        bridge.close()


def test_enter_records_intent_only_and_duplicate_is_idempotent():
    lv, _ = started()
    bridge, fake = _bridge(lv)
    try:
        _, _ = _post_initial(lv, bridge, fake)
        value = _action_value(fake.posts[0][1], ENTER_ACTION_ID)
        interaction = SlackInteraction(
            interaction_id="interaction-1",
            user_id="U123",
            action=SlackAction.ENTER,
            setup_id=value["s"],
            proposal_id=value["p"],
            approval_view_id=value["v"],
            interaction_wall_ns=NOW,
        )
        fake.emit(interaction)
        fake.emit(interaction)
        _process_click(lv, bridge, interaction)
        _wait(lambda: len(bridge.journal.entries) == 1)
        audit = bridge.journal.entries[0]
        assert audit.accepted_human_intent
        assert not audit.authorizes_execution
        assert audit.human_response["authorizes_execution"] is False
        assert "risk_eligibility_policy_not_implemented" in audit.result_codes
        assert "execution_layer_disabled" in audit.result_codes
        assert bridge.stats.duplicates == 1
        _wait(lambda: len(fake.updates) >= 1)
        assert _action_ids(fake.updates[-1][1]) == []
        assert "No order was sent" in json.dumps(fake.updates[-1][1].blocks)
    finally:
        bridge.close()


def test_reject_closes_human_workflow_and_sends_no_execution():
    lv, _ = started()
    bridge, fake = _bridge(lv)
    try:
        _, _ = _post_initial(lv, bridge, fake)
        value = _action_value(fake.posts[0][1], REJECT_ACTION_ID)
        interaction = SlackInteraction(
            interaction_id="interaction-reject",
            user_id="U123",
            action=SlackAction.REJECT,
            setup_id=value["s"],
            proposal_id=value["p"],
            approval_view_id=value["v"],
        )
        fake.emit(interaction)
        _process_click(lv, bridge, interaction)
        _wait(lambda: len(bridge.journal.entries) == 1)
        audit = bridge.journal.entries[0]
        assert audit.action == "REJECT" and audit.accepted_human_intent
        assert not audit.authorizes_execution
        _wait(lambda: len(fake.updates) >= 1)
        assert _action_ids(fake.updates[-1][1]) == []
    finally:
        bridge.close()


def test_old_slack_view_fails_closed_after_hold_update():
    lv, rec = started()
    bridge, fake = _bridge(lv)
    try:
        _, p0 = _post_initial(lv, bridge, fake)
        old = _action_value(fake.posts[0][1], ENTER_ACTION_ID)

        lv.at(606)
        lv.sc.depth(DEPTH, 0, 1, 1, (rec.candidate.entry_reference + 1) * TICK, 10)
        lv.pump()
        held = current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=lv.sc.wall)
        bridge.publish_decision(_record(JournalKind.TEMPORARY_HOLD_ENTERED, held, lv), held)
        _wait(lambda: len(fake.updates) == 1)
        _wait(lambda: bridge.displayed_view(p0.proposal_id).approval_view_id == held.approval_view_id)

        click = SlackInteraction(
            interaction_id="old-view",
            user_id="U123",
            action=SlackAction.ENTER,
            setup_id=old["s"],
            proposal_id=old["p"],
            approval_view_id=old["v"],
        )
        fake.emit(click)
        _process_click(lv, bridge, click, at=607)
        _wait(lambda: len(bridge.journal.entries) == 1)
        audit = bridge.journal.entries[0]
        assert not audit.accepted_human_intent
        assert "slack_view_stale" in audit.result_codes
        assert "enter_failed_closed" in audit.result_codes
        assert audit.current_approval_allowed_now is False
    finally:
        bridge.close()


def test_slack_post_failure_never_mutates_market_or_decision_state():
    lv, rec = started()
    fake = FakeSlackTransport(fail_posts=1)
    bridge, fake = _bridge(lv, fake)
    try:
        before_market = state_hash(lv.h.engine)
        before_decision = lv.drv.fingerprint()
        p = current_approval_payload(lv.h.engine, rec, now_wall_ns=NOW)
        bridge.publish_decision(_record(JournalKind.APPROVAL_VIEW_CREATED, p, lv), p)
        _wait(lambda: bridge.stats.post_failures == 1)
        assert state_hash(lv.h.engine) == before_market
        assert lv.drv.fingerprint() == before_decision
        assert bridge.message_ref(p.proposal_id) is None
    finally:
        bridge.close()


def test_publish_is_nonblocking_despite_slow_slack_network():
    lv, rec = started()
    fake = FakeSlackTransport(latency_s=0.25)
    bridge, fake = _bridge(lv, fake)
    try:
        p = current_approval_payload(lv.h.engine, rec, now_wall_ns=NOW)
        t0 = time.monotonic()
        bridge.publish_decision(_record(JournalKind.APPROVAL_VIEW_CREATED, p, lv), p)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.15, elapsed
        _wait(lambda: len(fake.posts) == 1)
    finally:
        bridge.close()


def test_update_failure_keeps_last_confirmed_displayed_view():
    lv, rec = started()
    fake = FakeSlackTransport(fail_updates=1)
    bridge, fake = _bridge(lv, fake)
    try:
        _, initial = _post_initial(lv, bridge, fake)
        lv.at(606)
        lv.sc.depth(DEPTH, 0, 1, 1, (rec.candidate.entry_reference + 1) * TICK, 10)
        lv.pump()
        held = current_approval_payload(lv.h.engine, lv.lc.get(rec.setup_id), now_wall_ns=lv.sc.wall)
        bridge.publish_decision(_record(JournalKind.TEMPORARY_HOLD_ENTERED, held, lv), held)
        _wait(lambda: bridge.stats.update_failures == 1)
        assert bridge.displayed_view(initial.proposal_id).approval_view_id == initial.approval_view_id
        assert held.approval_view_id != initial.approval_view_id
    finally:
        bridge.close()


def test_enter_after_expiry_fails_closed():
    lv, rec = started()
    bridge, fake = _bridge(lv)
    try:
        _, _ = _post_initial(lv, bridge, fake)
        value = _action_value(fake.posts[0][1], ENTER_ACTION_ID)
        lv.tick_until(630)
        click = SlackInteraction(
            interaction_id="after-expiry",
            user_id="U123",
            action=SlackAction.ENTER,
            setup_id=value["s"],
            proposal_id=value["p"],
            approval_view_id=value["v"],
        )
        fake.emit(click)
        raw = lv.sc.events[-1]
        bridge.after_event(raw, (), raw.recv_mono_ns)
        _wait(lambda: len(bridge.journal.entries) == 1)
        audit = bridge.journal.entries[0]
        assert not audit.accepted_human_intent
        assert audit.current_status == "EXPIRED"
        assert "enter_failed_closed" in audit.result_codes
    finally:
        bridge.close()


def test_enter_after_safety_block_fails_closed():
    lv, rec = started()
    bridge, fake = _bridge(lv)
    try:
        _, _ = _post_initial(lv, bridge, fake)
        value = _action_value(fake.posts[0][1], ENTER_ACTION_ID)
        lv.at(606)
        lv.sc.error(-1, 1100)
        lv.pump()
        click = SlackInteraction(
            interaction_id="after-block",
            user_id="U123",
            action=SlackAction.ENTER,
            setup_id=value["s"],
            proposal_id=value["p"],
            approval_view_id=value["v"],
        )
        fake.emit(click)
        raw = lv.sc.events[-1]
        bridge.after_event(raw, (), raw.recv_mono_ns)
        _wait(lambda: len(bridge.journal.entries) == 1)
        audit = bridge.journal.entries[0]
        assert not audit.accepted_human_intent
        assert audit.current_status == "BLOCKED"
        assert "enter_failed_closed" in audit.result_codes
    finally:
        bridge.close()


def test_unknown_proposal_id_fails_closed_and_is_audited():
    lv, rec = started()
    bridge, fake = _bridge(lv)
    try:
        _, p = _post_initial(lv, bridge, fake)
        click = SlackInteraction(
            interaction_id="unknown-proposal",
            user_id="U123",
            action=SlackAction.ENTER,
            setup_id=p.setup_id,
            proposal_id="P" + "0" * 23,
            approval_view_id=p.approval_view_id,
        )
        fake.emit(click)
        _process_click(lv, bridge, click)
        _wait(lambda: len(bridge.journal.entries) == 1)
        audit = bridge.journal.entries[0]
        assert not audit.accepted_human_intent
        assert "slack_view_not_displayed" in audit.result_codes
        assert "proposal_changed" in audit.result_codes
    finally:
        bridge.close()
