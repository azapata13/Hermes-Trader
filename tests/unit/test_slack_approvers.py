"""D2.8 Slack approver allowlist: who may act on a proposal. Fail closed; nothing is executed.

No real Slack user ID appears here: the IDs are synthetic test values.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes.config import DecisionConfig
from hermes.slack.approvers import (
    APPROVER_ENV,
    ApproverConfigError,
    ApproverPolicy,
    ApproverStatus,
)
from hermes.slack.bridge import SlackApprovalBridge
from hermes.slack.fake import FakeSlackTransport
from hermes.slack.journal import ApprovalJournal
from hermes.slack.protocol import (
    ENTER_ACTION_ID,
    REJECT_ACTION_ID,
    SlackAction,
    SlackInteraction,
)
from tests.unit.test_lifecycle import started
from tests.unit.test_slack_approval import (
    _action_ids,
    _action_value,
    _post_initial,
    _process_click,
    _wait,
)

APPROVER = "U0APPROVER1"          # synthetic
OUTSIDER = "U0OUTSIDER9"          # synthetic


# ------------------------------------------------------------------ policy (pure)

def test_policy_from_env():
    assert not ApproverPolicy.from_env({}).configured
    assert not ApproverPolicy.from_env({APPROVER_ENV: "  "}).configured
    p = ApproverPolicy.from_env({APPROVER_ENV: f" {APPROVER} , W0ENTERPRISE2 "})
    assert p.approver_ids == frozenset({APPROVER, "W0ENTERPRISE2"})
    assert p.describe() == "2 approver(s)" and APPROVER not in p.describe()   # never logs the IDs


@pytest.mark.parametrize("raw", ["felipe", f"{APPROVER},", f"{APPROVER},u0lower", "C0CHANNEL1", "U1", "<@U0ABC>"])
def test_malformed_allowlist_is_a_configuration_error_never_a_partial_list(raw):
    with pytest.raises(ApproverConfigError):
        ApproverPolicy.from_env({APPROVER_ENV: raw})


@pytest.mark.parametrize("user,expected", [
    (APPROVER, ApproverStatus.AUTHORIZED),
    (OUTSIDER, ApproverStatus.NOT_ALLOWED),
    ("", ApproverStatus.MALFORMED_USER),
    (None, ApproverStatus.MALFORMED_USER),
    ("u0approver1", ApproverStatus.MALFORMED_USER),
    ("B0BOTUSER1", ApproverStatus.MALFORMED_USER),
])
def test_policy_check(user, expected):
    assert ApproverPolicy(frozenset({APPROVER})).check(user) is expected


def test_missing_allowlist_never_authorizes_execution():
    st = ApproverPolicy(None).check(APPROVER)
    assert st is ApproverStatus.ALLOWLIST_MISSING and st.may_act and not st.may_authorize_execution
    assert ApproverStatus.AUTHORIZED.may_authorize_execution
    assert not ApproverStatus.NOT_ALLOWED.may_act and not ApproverStatus.MALFORMED_USER.may_act


# ------------------------------------------------------------------ bridge integration

def _bridge(lv, policy):
    fake = FakeSlackTransport()
    bridge = SlackApprovalBridge(SimpleNamespace(driver=lv.drv), lv.h.engine, DecisionConfig(), fake,
                                 ApprovalJournal(), approvers=policy)
    bridge.start()
    return bridge, fake


def _click(fake, action_id, user, iid, kind):
    v = _action_value(fake.posts[0][1], action_id)
    return SlackInteraction(interaction_id=iid, user_id=user, action=kind,
                            setup_id=v["s"], proposal_id=v["p"], approval_view_id=v["v"])


def _run(policy, clicks):
    """clicks: list of (action_id, user, kind). Returns (bridge, fake, entries)."""
    lv, _ = started()
    bridge, fake = _bridge(lv, policy)
    try:
        _post_initial(lv, bridge, fake)
        made = [_click(fake, a, u, f"i{n}", k) for n, (a, u, k) in enumerate(clicks)]
        for c in made:
            fake.emit(c)
        _process_click(lv, bridge, made[-1])
        _wait(lambda: len(bridge.journal.entries) == len(made))
        return bridge, fake, bridge.journal.entries
    finally:
        bridge.close()


ENTER = (ENTER_ACTION_ID, SlackAction.ENTER)
REJECT = (REJECT_ACTION_ID, SlackAction.REJECT)


def test_authorized_enter_is_recorded_as_intent_only():
    _br, _fake, (e,) = _run(ApproverPolicy(frozenset({APPROVER})), [(ENTER[0], APPROVER, ENTER[1])])
    assert e.accepted_human_intent and not e.authorizes_execution
    assert "approver_authorized" in e.result_codes
    assert "approver_not_authorized_for_execution" not in e.result_codes
    assert "execution_layer_disabled" in e.result_codes           # still never executable in V1
    assert e.human_response["authorizes_execution"] is False


@pytest.mark.parametrize("kind", [ENTER, REJECT])
def test_unauthorized_user_changes_nothing_and_is_audited(kind):
    bridge, fake, (e,) = _run(ApproverPolicy(frozenset({APPROVER})), [(kind[0], OUTSIDER, kind[1])])
    assert not e.accepted_human_intent and not e.authorizes_execution
    assert "approver_not_allowed" in e.result_codes and e.slack_user_id == OUTSIDER
    assert e.human_response is None
    assert bridge.stats.unauthorized == 1 and bridge.stats.accepted_enters == 0 and bridge.stats.rejects == 0
    assert fake.updates == []                                      # the proposal stays open for real approvers


def test_unauthorized_click_does_not_close_the_workflow_for_the_approver():
    _br, _fake, entries = _run(ApproverPolicy(frozenset({APPROVER})),
                                 [(REJECT[0], OUTSIDER, REJECT[1]), (ENTER[0], APPROVER, ENTER[1])])
    outsider, approver = entries
    assert not outsider.accepted_human_intent
    assert approver.accepted_human_intent and "human_workflow_closed" not in approver.result_codes


@pytest.mark.parametrize("user", ["", "not-a-slack-id", "u0lower"])
def test_malformed_user_fails_closed_even_without_allowlist(user):
    _br, _fake, (e,) = _run(ApproverPolicy(None), [(ENTER[0], user, ENTER[1])])
    assert not e.accepted_human_intent and "approver_id_malformed" in e.result_codes


def test_missing_allowlist_records_intent_but_flags_it_as_not_executable():
    _br, _fake, (e,) = _run(ApproverPolicy(None), [(ENTER[0], APPROVER, ENTER[1])])
    assert e.accepted_human_intent and not e.authorizes_execution
    assert {"approver_allowlist_missing", "approver_not_authorized_for_execution"} <= set(e.result_codes)


def test_missing_allowlist_still_lets_a_human_reject():
    _br, fake, (e,) = _run(ApproverPolicy(None), [(REJECT[0], APPROVER, REJECT[1])])
    assert e.accepted_human_intent and e.action == "REJECT"
    _wait(lambda: len(fake.updates) >= 1)
    assert _action_ids(fake.updates[-1][1]) == []


def test_duplicate_authorized_enter_is_idempotent():
    lv, _ = started()
    bridge, fake = _bridge(lv, ApproverPolicy(frozenset({APPROVER})))
    try:
        _post_initial(lv, bridge, fake)
        c = _click(fake, ENTER_ACTION_ID, APPROVER, "dup", SlackAction.ENTER)
        fake.emit(c)
        fake.emit(c)
        _process_click(lv, bridge, c)
        _wait(lambda: len(bridge.journal.entries) == 1)
        assert bridge.stats.duplicates == 1 and bridge.stats.accepted_enters == 1
    finally:
        bridge.close()


def test_second_authorized_enter_after_acceptance_fails_closed():
    _br, _fake, entries = _run(ApproverPolicy(frozenset({APPROVER, "U0APPROVER2"})),
                                 [(ENTER[0], APPROVER, ENTER[1]), (ENTER[0], "U0APPROVER2", ENTER[1])])
    first, second = entries
    assert first.accepted_human_intent
    assert not second.accepted_human_intent and "human_workflow_closed" in second.result_codes


def test_authorized_enter_after_expiry_fails_closed():
    lv, _ = started()
    bridge, fake = _bridge(lv, ApproverPolicy(frozenset({APPROVER})))
    try:
        _post_initial(lv, bridge, fake)
        c = _click(fake, ENTER_ACTION_ID, APPROVER, "late", SlackAction.ENTER)
        lv.tick_until(630)
        fake.emit(c)
        raw = lv.sc.events[-1]
        bridge.after_event(raw, (), raw.recv_mono_ns)
        _wait(lambda: len(bridge.journal.entries) == 1)
        e = bridge.journal.entries[0]
        assert not e.accepted_human_intent and e.current_status == "EXPIRED"
        assert "approver_authorized" in e.result_codes and "enter_failed_closed" in e.result_codes
    finally:
        bridge.close()


def test_authorized_enter_on_unknown_proposal_fails_closed():
    lv, _ = started()
    bridge, fake = _bridge(lv, ApproverPolicy(frozenset({APPROVER})))
    try:
        _, p = _post_initial(lv, bridge, fake)
        c = SlackInteraction(interaction_id="unknown", user_id=APPROVER, action=SlackAction.ENTER,
                             setup_id=p.setup_id, proposal_id="P" + "0" * 23, approval_view_id=p.approval_view_id)
        fake.emit(c)
        _process_click(lv, bridge, c)
        _wait(lambda: len(bridge.journal.entries) == 1)
        e = bridge.journal.entries[0]
        assert not e.accepted_human_intent and "slack_view_not_displayed" in e.result_codes
    finally:
        bridge.close()


def test_audit_json_never_contains_the_allowlist():
    _br, _fake, (e,) = _run(ApproverPolicy(frozenset({APPROVER, "U0SECRETLIST"})),
                              [(ENTER[0], OUTSIDER, ENTER[1])])
    assert "U0SECRETLIST" not in json.dumps(e.to_dict())


def test_malformed_allowlist_disables_slack_in_the_live_runtime(monkeypatch, tmp_path):
    import dataclasses

    from hermes.app.run_live import LiveRuntime
    from hermes.config import load_config
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("HERMES_SLACK_CHANNEL_ID", "C0TEST")
    monkeypatch.setenv(APPROVER_ENV, "felipe")
    cfg = load_config()
    cfg = dataclasses.replace(cfg, telemetry=dataclasses.replace(cfg.telemetry, log_directory=str(tmp_path)),
                              recorder=dataclasses.replace(cfg.recorder, directory=str(tmp_path / "rec")))
    rt = LiveRuntime(cfg, record=False)
    assert rt.slack_bridge is None and APPROVER_ENV in (rt.slack_config_error or "")
    assert "felipe" not in (rt.slack_config_error or "")         # the offending value is not echoed
