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


def _find_tws_window_id() -> int:
    configured = os.getenv("HERMES_TWS_WINDOW_ID", "").strip()

    if configured:
        return int(configured)

    swift = r"""
import Foundation
import CoreGraphics

let windows = CGWindowListCopyWindowInfo(
    [.optionOnScreenOnly, .excludeDesktopElements],
    kCGNullWindowID
) as! [[String: Any]]

for w in windows {
    let owner = w[kCGWindowOwnerName as String] as? String ?? ""
    let title = w[kCGWindowName as String] as? String ?? ""
    let id = w[kCGWindowNumber as String] as? Int ?? 0
    let layer = w[kCGWindowLayer as String] as? Int ?? -1

    let text = (owner + " " + title).lowercased()

    if layer == 0 &&
       (text.contains("trader workstation") ||
        text.contains("interactive brokers")) {
        print(id)
        exit(0)
    }
}

exit(2)
"""

    result = subprocess.run(
        ["/usr/bin/swift", "-"],
        input=swift,
        text=True,
        capture_output=True,
        timeout=15,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "could not find Trader Workstation window: "
            + (result.stderr.strip() or "no matching window")
        )

    return int(result.stdout.strip().splitlines()[0])


def _capture_tws(path: Path) -> None:
    window_id = _find_tws_window_id()
    path.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            "/usr/sbin/screencapture",
            "-x",
            f"-l{window_id}",
            str(path),
        ],
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
                        + "\n\nReturn at most 2 short lines, only if useful, exactly in this form:\n"
                        "ALIGNMENT: <what on screen supports the proposal>\n"
                        "CONFLICT: <what on screen contradicts it, or NONE>\n"
                        "Do not repeat the proposal. Use UNKNOWN when something cannot be verified visually."
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


_MAX_NOTE_CHARS = 160


def luna_lines(text: str | None) -> list[str]:
    """Keep only useful ALIGNMENT / CONFLICT lines (max 2); drop empty / NONE / UNKNOWN-only lines."""
    out: list[str] = []
    for line in (text or "").splitlines():
        t = line.strip().lstrip("-•* ").strip()
        key, _, val = t.partition(":")
        if key.strip().upper() not in ("ALIGNMENT", "CONFLICT"):
            continue
        v = val.strip()
        if not v or v.upper().rstrip(".") in ("NONE", "UNKNOWN", "N/A"):
            continue
        out.append(f"{key.strip().upper()}: {v[:_MAX_NOTE_CHARS]}")
    return out[:2]


def sol_lines(text: str | None) -> list[str]:
    """Sol is shown only when it reports a significant conflict/problem (anything but NONE)."""
    t = (text or "").strip()
    if not t or t.upper().rstrip(".") == "NONE":
        return []
    return [line.strip()[:_MAX_NOTE_CHARS] for line in t.splitlines() if line.strip()][:2]


def screenshot_caption(p: ApprovalPayload, luna_text: str | None = None, sol_text: str | None = None) -> str:
    entry = "-" if p.entry_reference is None or not p.units_per_point else f"{p.entry_reference / p.units_per_point:.2f}"
    lines = [f"📸 TWS · {p.symbol} {p.direction} @ {entry}"]
    lines += [f"🧠 {x}" for x in luna_lines(luna_text)]
    lines += [f"🧭 {x}" for x in sol_lines(sol_text)]
    return "\n".join(lines)


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
            "If there is NO significant conflict or problem, reply exactly NONE. "
            "Otherwise reply with at most 2 short lines naming only the conflict/problem; "
            "do not repeat the proposal.\n\n"
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

            self.transport.upload_file(
                ref,
                str(path),
                title=f"Hermès {p.symbol} {p.direction}",
                initial_comment=screenshot_caption(p, luna_text, sol_text),
            )

            self.completed += 1

        except Exception as exc:
            self.failures += 1
            log.exception(
                "visual enrichment failed for %s: %s",
                p.proposal_id,
                exc,
            )
