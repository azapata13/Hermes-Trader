"""V1 Pro Slack providers: History line and re-arm note, computed from RECORDED data only.

Both are read-only lookups called by the bridge's outbox thread at render time. They never gate an
approval, never change a payload and return ``None`` whenever the data is missing or insufficient
(History: fewer than 30 comparable recorded shadow setups => no line at all).
"""

from __future__ import annotations

from hermes.analytics.stats import HISTORY_MIN_SAMPLES, OUTCOME_DEFINITION, comparable
from hermes.slack.summary import HistoryStat


def history_for(tracker, rows_fn, payload, min_samples: int = HISTORY_MIN_SAMPLES) -> HistoryStat | None:
    """Shadow +1R rate of comparable recorded setups for this proposal, or None (< 30 / unavailable)."""
    view = tracker.get(payload.setup_id) if tracker is not None else None
    if view is None or not view.features:
        return None
    comp = comparable(view.features, list(rows_fn()), exclude_setup_id=payload.setup_id, min_samples=min_samples)
    if not comp.displayable:
        return None
    s = comp.stats
    return HistoryStat(wins=s.plus_1r, samples=s.samples, outcome_definition=OUTCOME_DEFINITION,
                       selection_definition=comp.selection_definition, source=comp.source,
                       outcome_label="+1R", shadow=True)


def rearm_note(tracker, payload) -> str | None:
    """One line for a re-armed candidate: it follows an earlier setup whose entry window closed."""
    view = tracker.get(payload.setup_id) if tracker is not None else None
    if view is None or not view.rearm_of:
        return None
    prior = tracker.get(view.rearm_of)
    if prior is None:
        return None
    upp = prior.units_per_point
    old = f"{prior.original_entry_units / upp:.2f}" if upp else str(prior.original_entry_units)
    return (f"↻ Re-arm of an earlier {prior.direction} setup (original entry {old} expired). "
            "Entry above is new and freshly validated.")


# ---------------------------------------------------------------------------- D8 interpretation worker

class InterpretationWorker:
    """Posts an LLM INTERPRETATION of a setup as a thread reply, off every critical thread.

    ``interpreter(view) -> list[str]`` must return already-guarded display lines
    (``hermes.analytics.llm_context.interpret``). The worker only ever calls ``transport.reply_text``;
    it has no access to approvals, the decision runtime, configuration or any broker object. Bounded
    queue: when full, requests are dropped (counted), never blocking the caller.
    """

    PREFIX = "🧭 Sol — interpretation only (no authority, no order):"

    def __init__(self, transport, interpreter, max_queue: int = 8) -> None:
        from queue import Queue
        self.transport = transport
        self.interpreter = interpreter
        self._q: Queue = Queue(maxsize=max_queue)
        self._thread = None
        self.submitted = 0
        self.dropped = 0
        self.posted = 0
        self.failures = 0

    def start(self) -> None:
        from threading import Thread
        if self._thread is None:
            self._thread = Thread(target=self._run, name="hermes-sol-interpretation", daemon=True)
            self._thread.start()

    def submit(self, ref, view) -> bool:
        from queue import Full
        if not hasattr(self.transport, "reply_text"):
            return False
        try:
            self._q.put_nowait((ref, view))
        except Full:
            self.dropped += 1
            return False
        self.submitted += 1
        return True

    def close(self, timeout: float = 3.0) -> None:
        if self._thread is None:
            return
        self._q.put(None)
        self._thread.join(timeout)
        self._thread = None

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            ref, view = item
            try:
                lines = self.interpreter(view)
                if lines and all(isinstance(x, str) for x in lines):
                    self.transport.reply_text(ref.channel_id, ref.ts, self.PREFIX + "\n" + "\n".join(lines))
                    self.posted += 1
            except Exception:  # noqa: BLE001 - interpretation is optional and never on the safety path
                self.failures += 1

    def summary(self) -> dict:
        return {"submitted": self.submitted, "posted": self.posted, "dropped": self.dropped, "failures": self.failures}
