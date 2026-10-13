"""Slack warns the operator when the traded contract approaches (or passed) its expiry.

Display only: the warning never changes a candidate, a status, an approval or the buttons.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from hermes.config import DecisionConfig
from hermes.decision.approval import current_approval_payload
from hermes.decision.runtime import JournalKind
from hermes.market.events import ContractResolvedEvent
from hermes.slack.bridge import SlackApprovalBridge
from hermes.slack.fake import FakeSlackTransport
from hermes.slack.journal import ApprovalJournal
from hermes.slack.protocol import ENTER_ACTION_ID
from hermes.slack.render import render_slack_message
from hermes.slack.summary import ROLL_WARN_DAYS, contract_roll_warning
from tests.unit.test_lifecycle import started
from tests.unit.test_slack_approval import NOW, _action_ids, _record, _wait


def _ns(y, m, d):
    return int(datetime(y, m, d, 15, 0, tzinfo=UTC).timestamp() * 1e9)


def test_no_warning_far_from_expiry_or_without_a_full_date():
    assert contract_roll_warning("MNQZ6", "20261218", _ns(2026, 10, 13)) is None
    assert contract_roll_warning("MNQZ6", "202612", _ns(2026, 12, 15)) is None       # month only: not computable
    assert contract_roll_warning("MNQZ6", "", _ns(2026, 12, 15)) is None
    assert contract_roll_warning("MNQZ6", "20261218", None) is None


def test_roll_window_today_and_expired():
    w = contract_roll_warning("MNQZ6", "20261218", _ns(2026, 12, 10))
    assert w and "CONTRACT ROLL" in w and "MNQZ6" in w and "2026-12-18" in w and "in 8 d" in w
    assert contract_roll_warning("MNQZ6", "20261218", _ns(2026, 12, 18 - ROLL_WARN_DAYS))
    assert contract_roll_warning("MNQZ6", "20261218", _ns(2026, 12, 18 - ROLL_WARN_DAYS - 1)) is None
    assert "EXPIRES TODAY" in contract_roll_warning("MNQZ6", "20261218", _ns(2026, 12, 18))
    assert "EXPIRED" in contract_roll_warning("MNQZ6", "20261218", _ns(2026, 12, 21))


def test_warning_is_display_only_and_keeps_the_buttons():
    lv, rec = started()
    p = current_approval_payload(lv.h.engine, rec, now_wall_ns=NOW)
    plain = render_slack_message(p)
    warned = render_slack_message(p, roll_warning="⚠ CONTRACT ROLL: MNQZ6 expires 2026-12-18 (in 8 d)")
    assert _action_ids(warned) == _action_ids(plain) and ENTER_ACTION_ID in _action_ids(warned)
    assert "CONTRACT ROLL" in json.dumps(warned.blocks, ensure_ascii=False)
    assert "CONTRACT ROLL" not in json.dumps(plain.blocks, ensure_ascii=False)


def _contract_event(expiry):
    return ContractResolvedEvent(seq=1, instrument_id=1, recv_mono_ns=1, recv_wall_ns=1, con_id=770561201,
                                 symbol="MNQ", local_symbol="MNQZ6", exchange="CME", expiry=expiry,
                                 multiplier="2", min_tick="0.25", market_rule_id=67)


def _post_with_contract(expiry):
    lv, rec = started()
    fake = FakeSlackTransport()
    b = SlackApprovalBridge(SimpleNamespace(driver=lv.drv), lv.h.engine, DecisionConfig(), fake, ApprovalJournal(),
                            hold_display_delay_s=0.0)
    b.start()
    try:
        b.after_event(SimpleNamespace(seq=1, recv_wall_ns=1), (_contract_event(expiry),), 1)
        p = current_approval_payload(lv.h.engine, rec, now_wall_ns=NOW)
        b.publish_decision(_record(JournalKind.APPROVAL_VIEW_CREATED, p, lv), p)
        _wait(lambda: len(fake.posts) == 1)
        return json.dumps(fake.posts[0][1].blocks, ensure_ascii=False), _action_ids(fake.posts[0][1])
    finally:
        b.close()


def test_bridge_warns_when_the_live_contract_is_in_its_roll_window():
    when = datetime.fromtimestamp(NOW / 1e9, tz=UTC).date()
    near = f"{when.year:04d}{when.month:02d}{when.day:02d}"                 # expires on the proposal's day
    text, buttons = _post_with_contract(near)
    assert "EXPIRES TODAY" in text and ENTER_ACTION_ID in buttons          # attention required, nothing blocked
    text, _ = _post_with_contract(f"{when.year + 1:04d}1218")
    assert "CONTRACT" not in text
