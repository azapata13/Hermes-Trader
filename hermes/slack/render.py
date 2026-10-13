"""Deterministic, trader-facing Slack Block Kit rendering of a Hermès approval payload.

Built to be read in 5–10 seconds: direction, entry, stop/risk, 5m/1m/30s, VWAP, volume intensity,
buyer/seller dominance, tape/DOM confirmation, at most 3 reasons, decisional warnings only,
historical stats only when truly backed by recorded data, and ENTER / REJECT.

No internal ids, reason codes, evaluation metadata, take-profit or data-feed caveats are shown:
the complete technical view stays in the decision journal, the approval journal and the logs.
The button values still carry the compact setup / proposal / view ids (not visible) so every click
is verified against the exact view that was displayed. Rendering is pure: no Slack SDK import, no
network and no decision-state mutation.
"""

from __future__ import annotations

import json

from hermes.analytics.outcome import disposition_text, shadow_result, shadow_result_lines
from hermes.decision.approval import ApprovalPayload
from hermes.slack.protocol import (
    ENTER_ACTION_ID,
    REJECT_ACTION_ID,
    SlackMessage,
    SlackWorkflowState,
)
from hermes.slack.summary import HistoryStat, hold_reason, summarize


def _esc(value: object) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _action_value(p: ApprovalPayload) -> str:
    # Slack button values are transport metadata, not a copy of the payload.
    return json.dumps(
        {"s": p.setup_id, "p": p.proposal_id, "v": p.approval_view_id},
        separators=(",", ":"),
        sort_keys=True,
    )


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _status_line(p: ApprovalPayload, state: SlackWorkflowState, banner: str | None) -> str | None:
    """One line, only when the proposal is NOT simply open and approvable."""
    if state is SlackWorkflowState.ACTIVE and p.status == "ACTIONABLE" and p.approval_allowed_now and not banner:
        return None
    icon = {
        SlackWorkflowState.ACTIVE: "🔄",
        SlackWorkflowState.HOLD: "⏸",
        SlackWorkflowState.ENTER_RECORDED: "✅",
        SlackWorkflowState.REJECTED: "⛔",
        SlackWorkflowState.CLOSED: "🔒",
    }[state]
    if banner:
        text = banner
    elif p.status != "ACTIONABLE":
        text = f"{p.status.title()} — proposal ended, ENTER disabled."
    else:
        text = "Not approvable right now — ENTER unavailable."
    reason = hold_reason(p)
    if reason and (state is SlackWorkflowState.HOLD or not p.approval_allowed_now) and p.status == "ACTIONABLE":
        text = f"{text} ({reason})"
    return f"{icon} {_esc(text)}"


def render_slack_message(
    p: ApprovalPayload,
    *,
    workflow_state: SlackWorkflowState = SlackWorkflowState.ACTIVE,
    banner: str | None = None,
    history: HistoryStat | None = None,
    note: str | None = None,
    roll_warning: str | None = None,
) -> SlackMessage:
    """Render the exact approval view the human is shown.

    Slack has no disabled-button state: ENTER is omitted whenever ``approval_allowed_now`` is false;
    REJECT remains available while the human workflow is open.
    """
    s = summarize(p, history)
    top = f"*{_esc(s.headline)}*\n{_esc(s.risk)}"
    status = _status_line(p, workflow_state, banner)
    if status:
        top += f"\n{status}"
    blocks: list[dict] = [
        _section(top),
        _section("\n".join(_esc(x) for x in (s.timeframes, s.context, s.confirmation))),
    ]
    if s.reasons:
        blocks.append(_section("\n".join(f"{i}) {_esc(r)}" for i, r in enumerate(s.reasons, 1))))
    warnings = ((roll_warning,) if roll_warning else ()) + tuple(s.warnings)
    if warnings:
        blocks.append(_section("\n".join(_esc(w) for w in warnings)))
    if s.history:
        blocks.append(_section(_esc(s.history)))
    if note:
        blocks.append(_section(_esc(note)))

    if workflow_state in (SlackWorkflowState.ACTIVE, SlackWorkflowState.HOLD):
        elements = []
        if p.status == "ACTIONABLE" and p.approval_allowed_now:
            elements.append({
                "type": "button",
                "action_id": ENTER_ACTION_ID,
                "text": {"type": "plain_text", "text": "ENTER", "emoji": True},
                "style": "primary",
                "value": _action_value(p),
            })
        elements.append({
            "type": "button",
            "action_id": REJECT_ACTION_ID,
            "text": {"type": "plain_text", "text": "REJECT", "emoji": True},
            "style": "danger",
            "value": _action_value(p),
        })
        blocks.append({"type": "actions", "elements": elements})

    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _esc(s.footer)}]})
    fallback = f"{s.headline} · {s.risk}" + (f" · {status}" if status else "") + " · Intent only — no order sent"
    return SlackMessage(fallback, tuple(blocks))


def render_text(message: SlackMessage) -> str:
    """Plain-text view of a rendered message (tests / logs): what the trader actually reads."""
    out: list[str] = []
    for b in message.blocks:
        if b["type"] == "section":
            out.append(b["text"]["text"])
        elif b["type"] == "context":
            out.extend(e["text"] for e in b["elements"])
        elif b["type"] == "actions":
            out.append("  ".join(f"[ {e['text']['text']} ]" for e in b["elements"]))
    return "\n\n".join(out)


# ---------------------------------------------------------------------------- V1 Pro setup messages
# Shown after the ENTRY WINDOW closed. They never carry buttons: an old entry is never available.

_SETUP_TITLE = {
    "WINDOW_CLOSED": "👁 SETUP MONITORING",
    "REARMED": "↻ SETUP RE-ARMED",
    "INVALIDATED": "✖ SETUP INVALIDATED",
    "OUTCOME_FINAL": "■ SETUP ENDED",
}


def _pts(view, units, signed: bool = False) -> str:
    v = view.points(units)
    if v is None:
        return "-"
    return f"{v:+.2f}" if signed else f"{v:.2f}"


def render_setup_message(view, kind: str, *, human: str | None = None) -> SlackMessage:
    """Monitoring / re-arm / invalidation / end-of-tracking view of a setup (no ENTER, ever)."""
    o = view.outcome
    title = _SETUP_TITLE.get(kind, "SETUP UPDATE")
    upp = view.units_per_point
    head = f"*{title} · {view.direction} {view.symbol}*"
    closed = view.window_close_status or ("INVALIDATED" if view.phase.value == "INVALIDATED" else "open")
    lines = [
        head,
        f"Original entry {_px_units(view.original_entry_units, upp)} · SL {_px_units(view.original_stop_units, upp)}"
        f" — entry window closed ({closed})",
        f"Now {_px_units(o.last_price_units, upp)} · MFE {_pts(view, o.mfe_units, True)} pt · "
        f"MAE {_pts(view, o.mae_units)} pt",
    ]
    if kind == "WINDOW_CLOSED":
        lines.append("Old entry NOT available. A fresh 30 s trigger is required for a new entry window.")
    elif kind == "REARMED":
        lines.append(f"New entry window at {_px_units(view.rearm_entry_units, upp)} — posted separately after a fresh "
                     "validation. This message has no entry.")
    elif kind == "INVALIDATED":
        lines.append("Stop / structure touched — setup closed.")
    elif kind == "OUTCOME_FINAL":
        r = o.r_wall_ns
        lines.append(f"{o.status.value.title()} · +1R {_yn(len(r) > 0 and r[0] is not None)} · "
                     f"+2R {_yn(len(r) > 1 and r[1] is not None)} · stop-first "
                     f"{_yn(o.stop_touched and not (r and r[0] is not None))}"
                     + (f" · {o.incomplete_reason}" if o.incomplete_reason else ""))
        # V1 Pro: SHADOW RESULT (points, +10/+20/+30, outcome rule) — never presented as a trade or P&L
        lines.append("")
        lines.extend(shadow_result_lines(shadow_result(view.row()),
                                         disposition_text(human, view.window_close_status)))
    if human:
        lines.append(f"Human: {human}")
    blocks = [_section("\n".join(_esc(x) for x in lines)),
              {"type": "context", "elements": [{"type": "mrkdwn", "text": _esc(
                  "Shadow tracking — no order sent, no entry available")}]}]
    fallback = f"{title} · {view.direction} {view.symbol} · no entry available · no order sent"
    return SlackMessage(fallback, tuple(blocks))


def _yn(x) -> str:
    return "yes" if x else "no"


def _px_units(units, upp) -> str:
    if units is None:
        return "-"
    return f"{units / upp:.2f}" if upp else f"{units} units"
