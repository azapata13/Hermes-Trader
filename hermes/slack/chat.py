from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
import os
import re
from pathlib import Path
from queue import Full, Queue
from threading import Lock, Thread
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


def _command_text(text: str) -> str:
    text = re.sub(r"<@[A-Z0-9]+>", "", text)
    return " ".join(text.strip().lower().split())


def _is_status_command(text: str) -> bool:
    return _command_text(text) in {
        "status",
        "statut",
        "état",
        "etat",
        "status hermes",
        "statut hermes",
    }


def _use_sol(question: str) -> bool:
    q = question.lower()
    terms = (
        "setup", "dom", "order flow", "orderflow", "time & sales",
        "tape", "structure", "risque", "risk", "régime", "regime",
        "compare", "pourquoi", "confirmation", "entrée", "entry", "stop",
    )
    return len(question) > 180 or any(t in q for t in terms)


class ConversationalWorker:
    def __init__(
        self,
        transport,
        context_provider: Callable[[], str],
        status_provider: Callable[[], str] | None = None,
        max_queue: int = 16,
    ) -> None:
        self.transport = transport
        self.context_provider = context_provider
        self.status_provider = status_provider

        self._queue: Queue[object] = Queue(maxsize=max_queue)
        self._thread: Thread | None = None

        self.received = 0
        self.completed = 0
        self.failures = 0
        self.dropped = 0
        self.capture_failures = 0
        self.duplicates = 0

        self._seen_lock = Lock()
        self._seen_order: deque[str] = deque(maxlen=512)
        self._seen_set: set[str] = set()

        self._journal = Path(
            os.getenv(
                "HERMES_SLACK_CHAT_JOURNAL",
                "~/hermes-data/logs/slack_chat.jsonl",
            )
        ).expanduser()

    def start(self) -> None:
        if self._thread is not None:
            return

        self._journal.parent.mkdir(parents=True, exist_ok=True)

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

    def _new_message(self, mention: SlackMention) -> bool:
        key = f"{mention.channel_id}:{mention.ts}"

        with self._seen_lock:
            if key in self._seen_set:
                return False

            if len(self._seen_order) == self._seen_order.maxlen:
                oldest = self._seen_order.popleft()
                self._seen_set.discard(oldest)

            self._seen_order.append(key)
            self._seen_set.add(key)

        return True

    def submit(self, mention: SlackMention) -> None:
        self.received += 1

        if not self._new_message(mention):
            self.duplicates += 1
            log.info("duplicate Slack mention ignored ts=%s", mention.ts)
            return

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
            "duplicates": self.duplicates,
            "capture_failures": self.capture_failures,
        }

    def _write_journal(
        self,
        mention: SlackMention,
        *,
        model: str,
        answer: str,
        screenshot_status: str,
        status: str,
    ) -> None:
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "channel_id": mention.channel_id,
            "user_id": mention.user_id,
            "message_ts": mention.ts,
            "thread_ts": mention.thread_ts,
            "question": mention.text,
            "model": model,
            "screenshot_status": screenshot_status,
            "answer": answer,
        }

        try:
            with self._journal.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as exc:
            log.warning("could not write Slack conversation journal: %s", exc)

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
        model = "unknown"
        screenshot_status = "unavailable"

        try:
            if _is_status_command(mention.text):
                if self.status_provider is None:
                    answer = "⚠️ Hermès status provider unavailable."
                else:
                    answer = self.status_provider()

                self.transport.reply_text(
                    mention.channel_id,
                    mention.thread_ts,
                    answer,
                )

                self._write_journal(
                    mention,
                    model="local-status",
                    answer=answer,
                    screenshot_status="not_requested",
                    status="completed",
                )

                self.completed += 1
                return

            self.transport.reply_text(
                mention.channel_id,
                mention.thread_ts,
                "👀 J'analyse le contexte Hermès...",
            )

            context = self.context_provider()

            screenshot = None

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

            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

            model = (
                os.getenv("HERMES_SOL_MODEL", "gpt-5.6-sol")
                if _use_sol(mention.text)
                else os.getenv("HERMES_LUNA_MODEL", "gpt-5.6-luna")
            )

            prompt = f"""
You are Hermès Trader conversational analyst.

You have NO execution authority.

Use deterministic Hermès context as the primary factual source.
The screenshot is secondary visual evidence.

CRITICAL DATA-QUALITY RULES:
- Read CONTEXT_FRESHNESS before interpreting market conditions.
- If context_status=STALE, explicitly state that the market context is stale.
- Do not describe stale observations as current.
- If context_status=WARMING_UP, say that the session context is incomplete.
- Never fabricate missing bars, VWAP, tape, BBO, DOM, probabilities or win rates.
- IBKR depth is Market-By-Price, never Market-By-Order.
- Never infer individual trader identity or hidden individual orders.
- Distinguish observed facts from interpretation.
- Respond in concise natural French.
- Do not issue or execute an order.

SCREENSHOT STATUS:
{screenshot_status}

CURRENT HERMÈS MARKET CONTEXT:
{context}

USER QUESTION:
{mention.text}

When relevant cover:
- 5m regime
- 1m setup
- 30s trigger
- VWAP / session
- BBO
- DOM / MBP
- tape / aggressor flow
- supporting evidence
- contradictory evidence
- uncertainty

Answer the question directly.
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

            self._write_journal(
                mention,
                model=model,
                answer=answer,
                screenshot_status=screenshot_status,
                status="completed",
            )

            self.completed += 1

        except Exception as exc:
            self.failures += 1

            log.exception(
                "Hermès conversational response failed: %s",
                exc,
            )

            self._write_journal(
                mention,
                model=model,
                answer=f"{type(exc).__name__}: {exc}",
                screenshot_status=screenshot_status,
                status="failed",
            )

            try:
                self.transport.reply_text(
                    mention.channel_id,
                    mention.thread_ts,
                    f"⚠️ Hermès conversation error: `{type(exc).__name__}`",
                )
            except Exception:
                pass
