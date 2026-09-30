from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
from queue import Full, Queue
from threading import Thread
from typing import Callable

from hermes.slack.enrichment import _capture_tws, _image_url

log = logging.getLogger("hermes.slack.chat")

_STOP = object()


@dataclass(frozen=True, slots=True)
class SlackMention:
    channel_id: str
    user_id: str
    text: str
    ts: str
    thread_ts: str


def _use_sol(question: str) -> bool:
    q = question.lower()

    terms = (
        "setup",
        "dom",
        "order flow",
        "orderflow",
        "time & sales",
        "tape",
        "structure",
        "risque",
        "risk",
        "régime",
        "regime",
        "compare",
        "pourquoi",
        "confirmation",
        "entrée",
        "entry",
        "stop",
    )

    return len(question) > 180 or any(t in q for t in terms)


class ConversationalWorker:
    def __init__(
        self,
        transport,
        context_provider: Callable[[], str],
        max_queue: int = 16,
    ) -> None:
        self.transport = transport
        self.context_provider = context_provider

        self._queue: Queue[object] = Queue(maxsize=max_queue)
        self._thread: Thread | None = None

        self.received = 0
        self.completed = 0
        self.failures = 0
        self.dropped = 0
        self.capture_failures = 0

    def start(self) -> None:
        if self._thread is not None:
            return

        self._thread = Thread(
            target=self._run,
            name="hermes-slack-chat",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if self._thread is None:
            return

        try:
            self._queue.put_nowait(_STOP)
        except Full:
            pass

        self._thread.join(timeout=3)
        self._thread = None

    def submit(self, mention: SlackMention) -> None:
        self.received += 1

        try:
            self._queue.put_nowait(mention)
        except Full:
            self.dropped += 1

    def summary(self) -> dict:
        return {
            "received": self.received,
            "completed": self.completed,
            "failures": self.failures,
            "dropped": self.dropped,
            "capture_failures": self.capture_failures,
        }

    def _run(self) -> None:
        while True:
            item = self._queue.get()

            try:
                if item is _STOP:
                    return

                assert isinstance(item, SlackMention)
                self._process(item)

            finally:
                self._queue.task_done()

    def _process(self, mention: SlackMention) -> None:
        try:
            self.transport.reply_text(
                mention.channel_id,
                mention.thread_ts,
                "👀 J'analyse le contexte Hermès...",
            )

            context = self.context_provider()

            screenshot = None
            screenshot_status = "unavailable"

            try:
                directory = Path(
                    os.getenv(
                        "HERMES_SCREENSHOT_DIR",
                        "~/hermes-data/screenshots",
                    )
                ).expanduser()

                screenshot = directory / "conversation-current.png"

                _capture_tws(screenshot)

                if screenshot.exists() and screenshot.stat().st_size > 0:
                    screenshot_status = "available"
                else:
                    screenshot = None

            except Exception as exc:
                self.capture_failures += 1
                screenshot = None

                log.warning(
                    "conversation TWS screenshot unavailable; "
                    "continuing with internal market context: %s",
                    exc,
                )

            from openai import OpenAI

            client = OpenAI(
                api_key=os.environ["OPENAI_API_KEY"]
            )

            model = (
                os.getenv(
                    "HERMES_SOL_MODEL",
                    "gpt-5.6-sol",
                )
                if _use_sol(mention.text)
                else os.getenv(
                    "HERMES_LUNA_MODEL",
                    "gpt-5.6-luna",
                )
            )

            prompt = f"""
You are Hermès Trader conversational analyst.

You have NO execution authority.

Use the deterministic Hermès market context below as the primary source.

The TWS screenshot is optional.
If it is unavailable, analyze only the internal Hermès data and do NOT complain
that you cannot see the screen.

Important:
- IBKR depth is Market-By-Price, not Market-By-Order.
- Never infer trader identity or hidden individual orders.
- Never invent unavailable information.
- Never invent a win rate or probability.
- Answer in concise natural French.
- Distinguish observed facts from interpretation.
- Do not issue or execute an order.

SCREENSHOT STATUS:
{screenshot_status}

CURRENT HERMÈS MARKET CONTEXT:
{context}

USER QUESTION:
{mention.text}

Answer the user's question directly.

When relevant cover:
- 5m regime
- 1m setup
- 30s trigger
- BBO
- DOM / MBP
- tape / aggressor flow
- VWAP / session context
- evidence supporting the setup
- evidence contradicting it
- what remains uncertain
"""

            content = [{
                "type": "input_text",
                "text": prompt,
            }]

            if screenshot is not None:
                content.append({
                    "type": "input_image",
                    "image_url": _image_url(screenshot),
                })

            response = client.responses.create(
                model=model,
                input=[{
                    "role": "user",
                    "content": content,
                }],
                max_output_tokens=650,
            )

            answer = response.output_text.strip()

            self.transport.reply_text(
                mention.channel_id,
                mention.thread_ts,
                f"🧠 *Hermès* · `{model}`\n{answer}",
            )

            self.completed += 1

        except Exception as exc:
            self.failures += 1

            log.exception(
                "Hermès conversational response failed: %s",
                exc,
            )

            try:
                self.transport.reply_text(
                    mention.channel_id,
                    mention.thread_ts,
                    f"⚠️ Hermès conversation error: `{type(exc).__name__}`",
                )
            except Exception:
                pass
