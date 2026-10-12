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
