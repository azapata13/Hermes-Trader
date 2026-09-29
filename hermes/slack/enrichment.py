from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from queue import Full, Queue
import subprocess
from threading import Thread

from hermes.decision.approval import ApprovalPayload
from hermes.slack.protocol import SlackMessageRef

log = logging.getLogger("hermes.slack.enrichment")
_STOP = object()


def _enabled(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _capture_tws(path: Path) -> None:
    script = '''
tell application "System Events"
    tell process "JavaApplicationStub"
        set {x, y} to position of front window
        set {w, h} to size of front window
        return (x as text) & "," & (y as text) & "," & (w as text) & "," & (h as text)
    end tell
end tell
'''
    coords = subprocess.check_output(
        ["osascript", "-e", script],
        text=True,
        timeout=5,
    ).strip()

    x, y, w, h = [int(v.strip()) for v in coords.split(",")]

    path.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["screencapture", "-x", f"-R{x},{y},{w},{h}", str(path)],
        check=True,
        timeout=10,
    )


def _image_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode()
    return "data:image/png;base64," + encoded


def _proposal_text(p: ApprovalPayload) -> str:
    return (
        f"{p.symbol} {p.direction}\n"
        f"status={p.status}\n"
        f"entry_units={p.entry_reference}\n"
        f"stop_units={p.proposed_stop}\n"
        f"risk_points={p.risk_points}\n"
        f"session_rth={p.session_rth}\n"
        f"market_data_ok={p.market_data_ok}\n"
        f"proposal_id={p.proposal_id}"
    )


def _analyze_luna(p: ApprovalPayload, path: Path) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    response = client.responses.create(
        model=os.getenv("HERMES_LUNA_MODEL", "gpt-5.6-luna"),
        input=[{
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "You are the visual observer for Hermès Trader. "
                        "You have NO execution authority. Review the TWS screenshot "
                        "and compare it with the deterministic proposal below.\n\n"
                        + _proposal_text(p)
                        + "\n\nReturn no more than 5 short lines:\n"
                        "VISUAL:\nALIGNMENT:\nCONFLICT:\nDATA QUALITY:\nNOTE:\n"
                        "Use UNKNOWN when something cannot be verified visually."
                    ),
                },
                {
                    "type": "input_image",
                    "image_url": _image_url(path),
                },
            ],
        }],
        max_output_tokens=220,
    )

    return response.output_text.strip()


def _needs_sol(p: ApprovalPayload) -> bool:
    if not p.market_data_ok:
        return True

    if any(r.severity.value in {"BLOCK", "HOLD", "CAUTION"} for r in p.reasons):
        return True

    votes = [getattr(c.vote, "value", str(c.vote)) for c in p.orderflow_evidence]

    opposite = "SHORT" if p.direction == "LONG" else "LONG"
    return opposite in votes


def _analyze_sol(p: ApprovalPayload, luna_text: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    response = client.responses.create(
        model=os.getenv("HERMES_SOL_MODEL", "gpt-5.6-sol"),
        input=(
            "You are the supervisory reviewer for Hermès Trader. "
            "You have NO broker access and NO execution authority. "
            "Review the deterministic proposal and Luna's visual observation. "
            "Identify contradictions, uncertainty, regime conflict, or data-quality "
            "concerns. Do not tell the human to ENTER or REJECT. "
            "Maximum 4 short lines.\n\n"
            f"Proposal:\n{_proposal_text(p)}\n\n"
            f"Luna:\n{luna_text}"
        ),
        max_output_tokens=180,
    )

    return response.output_text.strip()


class VisualEnrichmentWorker:
    def __init__(self, transport, max_queue: int = 32) -> None:
        self.transport = transport
        self._queue: Queue[object] = Queue(maxsize=max_queue)
        self._thread: Thread | None = None

        self.queued = 0
        self.dropped = 0
        self.completed = 0
        self.failures = 0

    @property
    def enabled(self) -> bool:
        return _enabled("HERMES_SCREENSHOT_ENABLED")

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return

        self._thread = Thread(
            target=self._run,
            name="hermes-visual-enrichment",
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

    def submit(self, proposal: ApprovalPayload, ref: SlackMessageRef) -> None:
        if not self.enabled:
            return

        try:
            self._queue.put_nowait((proposal, ref))
            self.queued += 1
        except Full:
            self.dropped += 1
            log.warning(
                "visual enrichment queue full for %s",
                proposal.proposal_id,
            )

    def summary(self) -> dict:
        return {
            "enabled": self.enabled,
            "queued": self.queued,
            "dropped": self.dropped,
            "completed": self.completed,
            "failures": self.failures,
        }

    def _run(self) -> None:
        while True:
            item = self._queue.get()

            try:
                if item is _STOP:
                    return

                proposal, ref = item
                self._process(proposal, ref)

            finally:
                self._queue.task_done()

    def _process(self, p: ApprovalPayload, ref: SlackMessageRef) -> None:
        try:
            directory = Path(
                os.getenv(
                    "HERMES_SCREENSHOT_DIR",
                    "~/hermes-data/screenshots",
                )
            ).expanduser()

            path = directory / f"{p.proposal_id}.png"

            _capture_tws(path)

            luna_text = None
            sol_text = None

            if _enabled("HERMES_AI_ENABLED") and os.getenv("OPENAI_API_KEY"):
                luna_text = _analyze_luna(p, path)

                if _enabled("HERMES_SOL_ON_COMPLEX", "1") and _needs_sol(p):
                    sol_text = _analyze_sol(p, luna_text)

            lines = [
                "📸 *Hermès automatic TWS capture*",
                "Informational only — deterministic safety remains authoritative.",
            ]

            if luna_text:
                lines.extend(["", "🧠 *Luna*", luna_text])

            if sol_text:
                lines.extend(["", "🧭 *Sol*", sol_text])

            self.transport.upload_file(
                ref,
                str(path),
                title=f"Hermès {p.symbol} {p.direction}",
                initial_comment="\n".join(lines),
            )

            self.completed += 1

        except Exception as exc:
            self.failures += 1
            log.exception(
                "visual enrichment failed for %s: %s",
                p.proposal_id,
                exc,
            )
