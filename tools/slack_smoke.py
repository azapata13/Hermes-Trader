"""Safe D1 Slack transport smoke test — posts one text message, never touches IBKR.

Usage after installing the ``slack`` extra and exporting the three D1 Slack
environment variables:

    python tools/slack_smoke.py
"""

from __future__ import annotations

import sys
import time

from hermes.slack.protocol import SlackMessage
from hermes.slack.socket_mode import SlackConfigError, SlackSettings, SlackSocketModeTransport


def main() -> int:
    try:
        settings = SlackSettings.from_env()
    except SlackConfigError as exc:
        print(f"SLACK CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    if settings is None:
        print(
            "Set SLACK_BOT_TOKEN, SLACK_APP_TOKEN and HERMES_SLACK_CHANNEL_ID first.",
            file=sys.stderr,
        )
        return 2

    transport = SlackSocketModeTransport(settings)
    try:
        transport.start(lambda interaction: None)
        ref = transport.post(
            SlackMessage(
                "Hermès D1 Slack transport smoke PASS — no order path.",
                (
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                "✅ *Hermès D1 Slack transport smoke PASS*\n"
                                "Socket Mode + `chat:write` are working. *No order path exists.*"
                            ),
                        },
                    },
                ),
            )
        )
        print(f"PASS channel={ref.channel_id} ts={ref.ts}")
        time.sleep(0.25)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
