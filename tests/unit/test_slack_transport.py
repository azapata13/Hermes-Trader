"""Slack transport start-up (live incident: a launchd-started Hermès ran with Slack silently OFF).

Root cause: ``SlackApprovalBridge.start`` tried ``transport.start`` exactly once. That call imports
slack_bolt, runs ``auth.test`` (``App(token=...)``) and ``apps.connections.open`` (``handler.connect()``),
so one transient failure at boot (network not ready, Slack API error) left the process alive with no
Socket Mode connection and nothing but a single log line. Mentions dropped for a channel mismatch were
also silent, so "no event received" could not be diagnosed. All of this is transport-only.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import types
from types import SimpleNamespace
from typing import ClassVar

import pytest

from hermes.config import DecisionConfig
from hermes.slack.bridge import SlackApprovalBridge
from hermes.slack.chat import SlackMention
from hermes.slack.fake import FakeSlackTransport
from hermes.slack.journal import ApprovalJournal
from hermes.slack.socket_mode import SlackSettings, SlackSocketModeTransport

BOT = "xoxb-test-bot-token-never-logged"
APP = "xapp-test-app-token-never-logged"


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class FlakyTransport(FakeSlackTransport):
    def __init__(self, failures, exc=None):
        super().__init__()
        self.failures, self.attempts = failures, 0
        self.exc = exc if exc is not None else ConnectionError("Slack API unreachable")

    def start(self, handler):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.exc
        super().start(handler)


def _bridge(transport, **kw):
    b = SlackApprovalBridge(SimpleNamespace(driver=None), None, DecisionConfig(), transport, ApprovalJournal(), **kw)
    b.start()
    return b


# ---------------------------------------------------------------------------- bridge: start / retry

def test_healthy_start_is_immediate_and_needs_no_retry_thread():
    t = FlakyTransport(0)
    b = _bridge(t)
    try:
        assert b.slack_connected and t.attempts == 1 and b._connector is None
        assert b.summary()["connected"] is True
    finally:
        b.close()


def test_transient_start_failures_are_retried_until_slack_is_up(caplog):
    caplog.set_level(logging.WARNING, logger="hermes.slack.bridge")
    t = FlakyTransport(2)
    t0 = time.monotonic()
    b = _bridge(t, connect_retry_initial_s=0.02, connect_retry_max_s=0.05)
    try:
        assert time.monotonic() - t0 < 0.5                       # never blocks the caller (market core)
        assert _wait(lambda: b.slack_connected)
        assert t.attempts == 3 and b.stats.connect_failures == 2
        msgs = [r.getMessage() for r in caplog.records]
        assert any("attempt 1" in m and "ConnectionError" in m and "market core continues" in m for m in msgs)
        assert any("started after 3 attempts" in m for m in msgs)
        t.emit(SimpleNamespace(interaction_id="x"))               # the interaction handler is wired after retry
        assert b.stats.interactions_received == 1
    finally:
        b.close()


def test_missing_sdk_is_reported_once_and_not_retried(caplog):
    caplog.set_level(logging.CRITICAL, logger="hermes.slack.bridge")
    t = FlakyTransport(99, ModuleNotFoundError("No module named 'slack_bolt'"))
    b = _bridge(t, connect_retry_initial_s=0.01)
    try:
        time.sleep(0.1)
        assert not b.slack_connected and t.attempts == 1 and b._connector is None
        crit = [r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL]
        assert crit and "slack-bolt==1.30.0" in crit[0] and sys.executable in crit[0]
    finally:
        b.close()


def test_close_stops_the_retry_loop_promptly():
    t = FlakyTransport(10**6)
    b = _bridge(t, connect_retry_initial_s=5.0)
    t0 = time.monotonic()
    b.close()
    assert time.monotonic() - t0 < 1.0 and b._connector is None and t.attempts == 1


# ---------------------------------------------------------------------------- Socket Mode transport (fake SDK)

class _FakeClient:
    def __init__(self):
        self.connected = True
        self.connects = 0

    def is_connected(self):
        return self.connected

    def connect(self):
        self.connects += 1
        self.connected = True


class _FakeApp:
    instances: ClassVar[list] = []

    def __init__(self, token):
        self.token = token
        self.listeners = {}
        self.client = SimpleNamespace(auth_test=lambda: {"user_id": "U0BOT", "user": "hermes", "team": "Felipe"})
        _FakeApp.instances.append(self)

    def _register(self, key):
        def deco(fn):
            self.listeners[key] = fn
            return fn
        return deco

    def action(self, action_id):
        return self._register(("action", action_id))

    def event(self, name):
        return self._register(("event", name))


class _FakeHandler:
    def __init__(self, app, app_token):
        self.app, self.app_token, self.client, self.closed = app, app_token, _FakeClient(), False

    def connect(self):
        self.client.connects += 1

    def close(self):
        self.closed = True


@pytest.fixture
def fake_sdk(monkeypatch):
    _FakeApp.instances = []
    bolt = types.ModuleType("slack_bolt")
    bolt.App = _FakeApp
    adapter = types.ModuleType("slack_bolt.adapter")
    sm = types.ModuleType("slack_bolt.adapter.socket_mode")
    sm.SocketModeHandler = _FakeHandler
    monkeypatch.setitem(sys.modules, "slack_bolt", bolt)
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter", adapter)
    monkeypatch.setitem(sys.modules, "slack_bolt.adapter.socket_mode", sm)
    return _FakeApp


def _transport():
    return SlackSocketModeTransport(SlackSettings(bot_token=BOT, app_token=APP, channel_id="C123"))


def test_start_registers_listeners_logs_the_connection_and_never_the_tokens(fake_sdk, caplog):
    caplog.set_level(logging.INFO, logger="hermes.slack.socket_mode")
    t = _transport()
    t.start(lambda i: None)
    try:
        app = fake_sdk.instances[0]
        assert {("event", "app_mention"), ("event", "message"), ("action", "hermes_enter"),
                ("action", "hermes_reject")} <= set(app.listeners)
        assert t._handler.client.connects == 1
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "Slack Socket Mode connected: bot U0BOT" in text and "channel C123" in text
        assert BOT not in text and APP not in text
    finally:
        t.close()
    assert t._handler is None


def test_mentions_are_logged_and_wrong_channel_drops_are_visible(fake_sdk, caplog):
    caplog.set_level(logging.INFO, logger="hermes.slack.socket_mode")
    got: list[SlackMention] = []
    t = _transport()
    t.set_mention_handler(got.append)
    t.start(lambda i: None)
    try:
        mention = fake_sdk.instances[0].listeners[("event", "app_mention")]
        mention({"channel": "C999", "user": "U1", "text": "<@U0BOT> status", "ts": "1.1"})
        mention({"channel": "C999", "user": "U1", "text": "<@U0BOT> status", "ts": "1.2"})
        mention({"channel": "C123", "user": "U1", "text": "<@U0BOT> status", "ts": "1.3"})
        mention({"channel": "C123", "bot_id": "B1", "text": "echo", "ts": "1.4"})
        assert [m.ts for m in got] == ["1.3"] and got[0].thread_ts == "1.3"
        assert (t.mentions_received, t.mentions_ignored) == (1, 3)
        warn = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert warn == ["Slack mention ignored: channel C999 is not HERMES_SLACK_CHANNEL_ID C123"]   # once
        assert any("Slack mention received ts=1.3" in r.getMessage() for r in caplog.records)
    finally:
        t.close()


def test_watchdog_logs_disconnection_and_reconnects_when_the_sdk_did_not(fake_sdk, caplog):
    caplog.set_level(logging.INFO, logger="hermes.slack.socket_mode")
    t = _transport()
    t.WATCH_INTERVAL_S, t.RECONNECT_AFTER_S = 0.01, 0.03
    t.start(lambda i: None)
    try:
        client = t._handler.client
        client.connected = False
        assert _wait(lambda: client.connects >= 2)               # 1 = initial connect, then the forced one
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "Slack Socket Mode DISCONNECTED" in text and "reconnecting" in text
    finally:
        t.close()
    assert not any(th.name == "hermes-slack-watchdog" and th.is_alive() for th in threading.enumerate())


def test_auth_failure_in_app_construction_propagates_to_the_bridge_retry(fake_sdk):
    class FailingApp(_FakeApp):
        def __init__(self, token):
            raise RuntimeError("auth.test failed: network unreachable")

    sys.modules["slack_bolt"].App = FailingApp
    t = _transport()
    with pytest.raises(RuntimeError):
        t.start(lambda i: None)
    assert t._app is None and t._handler is None                 # nothing half-started; a retry starts clean


# ---------------------------------------------------------------------------- channel messages without @mention

def test_channel_messages_are_routed_and_hermes_own_messages_never_re_enter(fake_sdk):
    got: list[SlackMention] = []
    t = _transport()
    t.set_channel_message_handler(got.append)
    t.start(lambda i: None)
    try:
        message = fake_sdk.instances[0].listeners[("event", "message")]
        assert t.bot_user_id == "U0BOT"
        message({"channel": "C123", "user": "U1", "text": "Qu'est-ce qui doit changer pour un LONG ?", "ts": "1.1"})
        message({"channel": "C123", "user": "U1", "text": "Et la VWAP ?", "ts": "1.2", "thread_ts": "1.1"})
        # never a question: Hermès' own posts / replies, other bots, edits & system events, other channels
        message({"channel": "C123", "user": "U0BOT", "bot_id": "B1", "text": "🧠 Hermès · réponse", "ts": "1.3",
                 "thread_ts": "1.1"})
        message({"channel": "C123", "user": "U0BOT", "text": "👀 J'analyse le contexte Hermès...", "ts": "1.4"})
        message({"channel": "C123", "bot_id": "B2", "subtype": "bot_message", "text": "other bot", "ts": "1.5"})
        message({"channel": "C123", "user": "U1", "subtype": "message_changed", "text": "edit", "ts": "1.6"})
        message({"channel": "C123", "user": "U1", "subtype": "channel_join", "text": "joined", "ts": "1.7"})
        message({"channel": "C999", "user": "U1", "text": "wrong channel", "ts": "1.8"})
        message({"channel": "C123", "user": "U1", "text": "   ", "ts": "1.9"})
        assert [(m.ts, m.thread_ts) for m in got] == [("1.1", "1.1"), ("1.2", "1.1")]
        assert got[0].text == "Qu'est-ce qui doit changer pour un LONG ?" and got[0].channel_id == "C123"
    finally:
        t.close()


def test_bridge_wires_channel_messages_to_the_operator_only_conversation_and_never_to_approvals(tmp_path):
    from hermes.slack.approvers import ApproverPolicy
    from hermes.slack.chat import LlmResult

    class ChannelTransport(FakeSlackTransport):
        def set_channel_message_handler(self, handler):
            self.on_channel_message = handler

    t = ChannelTransport()
    b = SlackApprovalBridge(SimpleNamespace(driver=None), None, DecisionConfig(), t, ApprovalJournal(),
                            approvers=ApproverPolicy(frozenset({"U0AAAA1B2CD"})))
    calls = []
    b.chat.responder = lambda **kw: calls.append(kw) or LlmResult("Il faut un régime 5m LONG.")
    b.chat.screenshot_fn = lambda p: (_ for _ in ()).throw(RuntimeError("no TWS window"))
    b.chat._journal = tmp_path / "chat.jsonl"
    b.start()
    try:
        assert t.on_channel_message == b.chat.submit_channel_message
        t.on_channel_message(SlackMention("C123", "U0ZZZZ9Y8XW", "Qu'est-ce qui doit changer ?", "2.1", "2.1"))
        t.on_channel_message(SlackMention("C123", "U0AAAA1B2CD", "Qu'est-ce qui doit changer pour un LONG ?",
                                          "2.2", "2.2"))
        b.chat._queue.join()
        assert len(calls) == 1 and [r[1] for r in t.replies if r[2].startswith("🧠")] == ["2.2"]
        assert len(b.journal.entries) == 0 and b.stats.interactions_received == 0   # conversation ≠ approval
        assert t.posts == [] and t.updates == []
    finally:
        b.close()
