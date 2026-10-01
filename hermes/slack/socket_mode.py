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

    def __init__(self, settings: SlackSettings) -> None:
        self.settings = settings
        self.channel_id = settings.channel_id
        self._app = None
        self._handler = None
        self._on_interaction: InteractionHandler | None = None
        self._on_mention = None

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
            # message.channels is subscribed for future conversational features.
            # app_mention is handled separately above.
            return

        handler = SocketModeHandler(app, self.settings.app_token)
        handler.connect()
        self._app = app
        self._handler = handler

    def close(self) -> None:
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

    def _handle_mention(self, event: object) -> None:
        if self._on_mention is None or not isinstance(event, dict):
            return

        channel = str(event.get("channel") or "")

        if channel != self.channel_id:
            return

        if event.get("bot_id"):
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
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            log.warning("ignored invalid Slack interaction: %s", exc)
            return
        self._on_interaction(interaction)
