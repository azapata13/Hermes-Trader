"""Slack interaction parsing fails closed: anything malformed is ignored (logged), never forwarded
as an approval intent and never raised into the Slack SDK thread. Pure: no Slack SDK, no network."""

from __future__ import annotations

import json

import pytest

from hermes.slack.protocol import SlackAction
from hermes.slack.socket_mode import SlackSettings, SlackSocketModeTransport

S, P, V = "S" + "a" * 23, "P" + "b" * 23, "V" + "c" * 23
GOOD = json.dumps({"s": S, "p": P, "v": V})


def _transport():
    t = SlackSocketModeTransport(SlackSettings("xoxb-test", "xapp-test", "C0TEST"))
    got = []
    t._on_interaction = got.append
    return t, got


def _body(value=GOOD, user=None, team=None, actions=None):
    b = {"actions": actions if actions is not None else [{"value": value, "action_ts": "1790000000.123456",
                                                           "action_id": "hermes_enter"}],
         "user": {"id": "U0HUMAN"} if user is None else user}
    if team is not None:
        b["team"] = team
    return b


def test_valid_interaction_is_forwarded_once_with_exact_ids():
    t, got = _transport()
    t._handle_body(_body(), SlackAction.ENTER)
    assert len(got) == 1
    i = got[0]
    assert (i.setup_id, i.proposal_id, i.approval_view_id, i.user_id) == (S, P, V, "U0HUMAN")
    assert i.action is SlackAction.ENTER


@pytest.mark.parametrize("body", [
    "not-a-dict",
    {},
    _body(actions=[]),
    _body(actions="x"),
    _body(actions=["x"]),
    _body(value=None),
    _body(value=123),
    _body(value="not json"),
    _body(value=json.dumps([S, P, V])),
    _body(value=json.dumps({"s": S, "p": P})),                       # missing view id
    _body(value=json.dumps({"s": S, "p": P, "v": V, "x": 1})),       # extra key
    _body(value=json.dumps({"s": P, "p": S, "v": V})),               # swapped identities
    _body(value=json.dumps({"s": S, "p": P, "v": V[:-1]})),          # truncated id
    _body(value=json.dumps({"s": S, "p": P, "v": 7})),
    _body(user={}),                                                  # no user id
    _body(user="U0HUMAN"),                                           # user not an object
    _body(team="T0"),                                                # team not an object
], ids=lambda b: (json.dumps(b)[:60] if not isinstance(b, str) else b))
def test_malformed_interaction_is_ignored_never_forwarded_never_raised(body):
    t, got = _transport()
    for action in (SlackAction.ENTER, SlackAction.REJECT):
        t._handle_body(body, action)                 # must not raise into the Slack SDK thread
    assert got == []
