"""Secrets never reach logs, summaries or audit records (Slack tokens, OpenAI key, approver list)."""

from __future__ import annotations

import dataclasses
import json
import logging

from hermes.app.run_live import LiveRuntime
from hermes.config import load_config

BOT = "xoxb-SECRET-BOT-TOKEN-123"
APP = "xapp-SECRET-APP-TOKEN-456"
OPENAI = "sk-SECRET-OPENAI-789"
APPROVERS = "U0SECRETAPPROVER"


def test_runtime_startup_and_slack_summary_do_not_leak_secrets(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SLACK_BOT_TOKEN", BOT)
    monkeypatch.setenv("SLACK_APP_TOKEN", APP)
    monkeypatch.setenv("HERMES_SLACK_CHANNEL_ID", "C0TEST")
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI)
    monkeypatch.setenv("HERMES_SLACK_APPROVER_IDS", APPROVERS)
    caplog.set_level(logging.DEBUG)
    cfg = load_config()
    cfg = dataclasses.replace(cfg, telemetry=dataclasses.replace(cfg.telemetry, log_directory=str(tmp_path)),
                              recorder=dataclasses.replace(cfg.recorder, directory=str(tmp_path / "rec")))
    rt = LiveRuntime(cfg, record=False)
    assert rt.slack_bridge is not None
    text = "\n".join(r.getMessage() for r in caplog.records) + json.dumps(rt.slack_bridge.summary(), default=str)
    for secret in (BOT, APP, OPENAI, APPROVERS):
        assert secret not in text
    assert "1 approver(s)" in text                     # what IS logged: a count
