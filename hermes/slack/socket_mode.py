"""Real Slack Socket Mode transport for D1.

The Slack SDK is an OPTIONAL dependency and is imported lazily only when all
three required environment variables are present.  No public HTTP endpoint is
opened: Socket Mode initiates an outbound WebSocket connection.

Required:
    SLACK_BOT_TOKEN          xoxb-...  (bot scope: chat:write)
    SLACK_APP_TOKEN          xapp-...  (app-level scope: connections:write)
    HERMES_SLACK_CHANNEL_ID  channel where the bot is already invited
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from hermes.slack.chat import SlackMention
from hermes.slack.protocol import (
    ENTER_ACTION_ID,
    REJECT_ACTION_ID,
    InteractionHandler,
    SlackAction,
    SlackInteraction,
    SlackMessage,
    SlackMessageRef,
)

log = logging.getLogger("hermes.slack.socket_mode")


class SlackConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SlackSettings:
    bot_token: str
    app_token: str
    channel_id: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SlackSettings | None:
        env = os.environ if env is None else env
        names = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "HERMES_SLACK_CHANNEL_ID")
        vals = {name: (env.get(name) or "").strip() for name in names}
        present = [name for name, value in vals.items() if value]
        if not present:
            return None
        missing = [name for name, value in vals.items() if not value]
        if missing:
            raise SlackConfigError(
                "partial Slack configuration; set all of "
                "SLACK_BOT_TOKEN, SLACK_APP_TOKEN, HERMES_SLACK_CHANNEL_ID "
                f"(missing: {', '.join(missing)})"
            )
        if not vals["SLACK_BOT_TOKEN"].startswith("xoxb-"):
            raise SlackConfigError("SLACK_BOT_TOKEN must be a bot token starting with xoxb-")
        if not vals["SLACK_APP_TOKEN"].startswith("xapp-"):
            raise SlackConfigError("SLACK_APP_TOKEN must be an app-level token starting with xapp-")
        if any(ch.isspace() for ch in vals["HERMES_SLACK_CHANNEL_ID"]):
            raise SlackConfigError("HERMES_SLACK_CHANNEL_ID must be a channel ID, not a name")
        return cls(
            bot_token=vals["SLACK_BOT_TOKEN"],
            app_token=vals["SLACK_APP_TOKEN"],
            channel_id=vals["HERMES_SLACK_CHANNEL_ID"],
        )


def _wall_ns(action_ts: object) -> int | None:
    if not isinstance(action_ts, str) or not action_ts:
        return None
    try:
        return int(Decimal(action_ts) * 1_000_000_000)
    except (InvalidOperation, ValueError):
        return None


def _parse_value(value: object) -> tuple[str, str, str]:
    if not isinstance(value, str):
        raise ValueError("Slack action value is not a string")  # noqa: TRY004 - all malformed values rejected alike
    obj = json.loads(value)
    if not isinstance(obj, dict) or set(obj) != {"s", "p", "v"}:
        raise ValueError("Slack action value has an unexpected shape")
    s, p, v = obj["s"], obj["p"], obj["v"]
    if not (
        isinstance(s, str)
        and isinstance(p, str)
        and isinstance(v, str)
        and len(s) == len(p) == len(v) == 24
        and s.startswith("S")
        and p.startswith("P")
        and v.startswith("V")
    ):
        raise ValueError("Slack action contains invalid Hermès identities")
    return s, p, v


def _interaction_id(body: dict, action: dict) -> str:
    # Stable across a redelivery of the same Slack action; transport metadata only.
    parts = (
        (body.get("team") or {}).get("id", ""),
        (body.get("user") or {}).get("id", ""),
        (body.get("container") or {}).get("message_ts", ""),
        action.get("action_ts", ""),
        action.get("action_id", ""),
        action.get("value", ""),
    )
    return hashlib.sha256("\x1f".join(map(str, parts)).encode("utf-8")).hexdigest()[:32]


class SlackSocketModeTransport:
    """Slack Bolt / Socket Mode transport.

    ``start`` connects but does not block the caller. Bolt's socket/client
    threads receive interactions and ACK them before forwarding compact intent
    data to Hermès.
    """

    WATCH_INTERVAL_S = 30.0          # Socket Mode connection check period
    RECONNECT_AFTER_S = 120.0        # force a reconnect if still down after the SDK's own auto-reconnect

    def __init__(self, settings: SlackSettings) -> None:
        self.settings = settings
        self.channel_id = settings.channel_id
        self._app = None
        self._handler = None
        self._watch_stop = threading.Event()
        self._watch: threading.Thread | None = None
        self._ignored_channels: set[str] = set()
        self.mentions_received = 0
        self.mentions_ignored = 0
        self._on_interaction: InteractionHandler | None = None
        self._on_mention = None
        self._on_channel_message = None
        self.bot_user_id: str | None = None          # from auth.test: our own messages never re-enter

    def start(self, on_interaction: InteractionHandler) -> None:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler

        self._on_interaction = on_interaction
        app = App(token=self.settings.bot_token)

        @app.action(ENTER_ACTION_ID)
        def _enter(ack, body):
            ack()
            self._handle_body(body, SlackAction.ENTER)

        @app.action(REJECT_ACTION_ID)
        def _reject(ack, body):
            ack()
            self._handle_body(body, SlackAction.REJECT)

        @app.event("app_mention")
        def _mention(event):
            self._handle_mention(event)

        @app.event("message")
        def _message(event):
            # Plain messages in the configured channel (top-level or in a thread), no @mention needed.
            # The worker answers the authorized operator only; Hermès' own messages never re-enter.
            self._handle_channel_message(event)

        handler = SocketModeHandler(app, self.settings.app_token)
        handler.connect()
        self._app = app
        self._handler = handler
        try:
            who = app.client.auth_test()
            self.bot_user_id = str(who.get("user_id") or "") or None
            log.info("Slack Socket Mode connected: bot %s (%s), team %s; listening in channel %s",
                     who.get("user_id"), who.get("user"), who.get("team"), self.channel_id)
        except Exception as exc:  # noqa: BLE001 - identity log only; the connection itself succeeded
            log.info("Slack Socket Mode connected; listening in channel %s (identity lookup failed: %s)",
                     self.channel_id, type(exc).__name__)
        self._watch_stop.clear()
        self._watch = threading.Thread(target=self._watchdog, name="hermes-slack-watchdog", daemon=True)
        self._watch.start()

    def _connected(self) -> bool | None:
        client = getattr(self._handler, "client", None)
        try:
            return bool(client.is_connected()) if client is not None else None
        except Exception:  # noqa: BLE001 - unknown state is reported as such
            return None

    def _watchdog(self) -> None:
        """Log Socket Mode connection transitions; if the SDK's auto-reconnect has not restored the
        connection after RECONNECT_AFTER_S, reconnect explicitly. Never touches the market core."""
        down_since: float | None = None
        last: bool | None = True
        while not self._watch_stop.wait(self.WATCH_INTERVAL_S):
            up = self._connected()
            if up is None:
                continue
            if up != last:
                (log.info if up else log.warning)("Slack Socket Mode %s", "reconnected" if up else "DISCONNECTED")
                last = up
            if up:
                down_since = None
                continue
            now = time.monotonic()
            down_since = down_since or now
            if now - down_since >= self.RECONNECT_AFTER_S:
                log.warning("Slack Socket Mode still down after %.0fs; reconnecting", now - down_since)
                try:
                    self._handler.client.connect()
                    down_since = None
                except Exception as exc:  # noqa: BLE001 - retried at the next watchdog period
                    log.error("Slack Socket Mode reconnect failed: %s: %s", type(exc).__name__, exc)

    def close(self) -> None:
        self._watch_stop.set()
        if self._watch is not None:
            self._watch.join(2)
            self._watch = None
        handler = self._handler
        self._handler = None
        self._app = None
        if handler is not None:
            handler.close()

    def post(self, message: SlackMessage) -> SlackMessageRef:
        app = self._require_app()
        response = app.client.chat_postMessage(
            channel=self.channel_id,
            text=message.text,
            blocks=list(message.blocks),
        )
        return SlackMessageRef(self.channel_id, str(response["ts"]))

    def update(self, ref: SlackMessageRef, message: SlackMessage) -> None:
        app = self._require_app()
        app.client.chat_update(
            channel=ref.channel_id,
            ts=ref.ts,
            text=message.text,
            blocks=list(message.blocks),
        )

    def upload_file(
        self,
        ref: SlackMessageRef,
        path: str,
        *,
        title: str,
        initial_comment: str,
    ) -> None:
        app = self._require_app()
        app.client.files_upload_v2(
            channel=ref.channel_id,
            thread_ts=ref.ts,
            file=path,
            title=title,
            initial_comment=initial_comment,
        )

    def set_mention_handler(self, handler) -> None:
        self._on_mention = handler

    def reply_text(
        self,
        channel_id: str,
        thread_ts: str,
        text: str,
    ) -> None:
        app = self._require_app()
        app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
        )

    def set_channel_message_handler(self, handler) -> None:
        self._on_channel_message = handler

    def _handle_channel_message(self, event: object) -> None:
        if self._on_channel_message is None or not isinstance(event, dict):
            return
        # bot posts (ours included), edits/deletes/joins (any subtype) and other channels: never a question
        if event.get("bot_id") or event.get("subtype") or str(event.get("channel") or "") != self.channel_id:
            return
        ts = str(event.get("ts") or "")
        text, user_id = str(event.get("text") or "").strip(), str(event.get("user") or "")
        if not (ts and text and user_id) or (self.bot_user_id and user_id == self.bot_user_id):
            return
        self._on_channel_message(SlackMention(channel_id=self.channel_id, user_id=user_id, text=text, ts=ts,
                                              thread_ts=str(event.get("thread_ts") or ts)))

    def _handle_mention(self, event: object) -> None:
        if self._on_mention is None or not isinstance(event, dict):
            return

        channel = str(event.get("channel") or "")

        if channel != self.channel_id:
            self.mentions_ignored += 1
            if channel not in self._ignored_channels:          # once per channel: visible, never noisy
                self._ignored_channels.add(channel)
                log.warning("Slack mention ignored: channel %s is not HERMES_SLACK_CHANNEL_ID %s",
                            channel or "?", self.channel_id)
            return

        if event.get("bot_id"):
            self.mentions_ignored += 1
            return

        text = str(event.get("text") or "").strip()
        ts = str(event.get("ts") or "")
        user_id = str(event.get("user") or "")

        if not text or not ts or not user_id:
            return

        mention = SlackMention(
            channel_id=channel,
            user_id=user_id,
            text=text,
            ts=ts,
            thread_ts=str(event.get("thread_ts") or ts),
        )
        self.mentions_received += 1
        log.info("Slack mention received ts=%s user=%s thread=%s", ts, user_id, mention.thread_ts)
        self._on_mention(mention)

    def _require_app(self):
        if self._app is None:
            raise RuntimeError("Slack Socket Mode transport is not started")
        return self._app

    def _handle_body(self, body: object, action_kind: SlackAction) -> None:
        if self._on_interaction is None or not isinstance(body, dict):
            return
        try:
            actions = body.get("actions") or ()
            if not actions or not isinstance(actions[0], dict):
                raise ValueError("Slack interaction has no action")
            action = actions[0]
            setup_id, proposal_id, approval_view_id = _parse_value(action.get("value"))
            user_id = str((body.get("user") or {}).get("id") or "")
            if not user_id:
                raise ValueError("Slack interaction has no user id")
            interaction = SlackInteraction(
                interaction_id=_interaction_id(body, action),
                user_id=user_id,
                action=action_kind,
                setup_id=setup_id,
                proposal_id=proposal_id,
                approval_view_id=approval_view_id,
                interaction_wall_ns=_wall_ns(action.get("action_ts")),
            )
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            # fail closed: never forwarded as an intent, never raised into the Slack SDK thread
            log.warning("ignored invalid Slack interaction: %s", exc)
            return
        self._on_interaction(interaction)
