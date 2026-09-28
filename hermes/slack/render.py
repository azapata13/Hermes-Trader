"""Deterministic Slack Block Kit rendering for Hermès approval payloads.

Rendering is pure: no Slack SDK import, no network and no decision-state mutation.
"""

from __future__ import annotations

import json

from hermes.decision.approval import ApprovalPayload, TimeframeSummary
from hermes.decision.reasons import Severity
from hermes.slack.protocol import (
    ENTER_ACTION_ID,
    REJECT_ACTION_ID,
    SlackMessage,
    SlackWorkflowState,
)


def _esc(value: object) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _short(value: str) -> str:
    return value if len(value) <= 13 else value[:9] + "…" + value[-3:]


def _clip(value: object, limit: int = 300) -> str:
    text = _esc(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _px(units: int | None, units_per_point: int | None) -> str:
    if units is None:
        return "-"
    if not units_per_point:
        return f"{units} units"
    return f"{units / units_per_point:.2f}"


def _tf(t: TimeframeSummary) -> str:
    net = "-" if t.net_change_units is None else f"{t.net_change_units:+d} ticks"
    return (
        f"*{_esc(t.stage)}* `{_esc(t.result)}` · bars {t.bars_used} · "
        f"net {net} · Δ {_esc(t.known_delta):s} · "
        f"B/S/U {t.buy_volume}/{t.sell_volume}/{t.unknown_volume}"
    )


def _action_value(p: ApprovalPayload) -> str:
    # Slack button values are transport metadata, not a copy of the payload.
    return json.dumps(
        {"s": p.setup_id, "p": p.proposal_id, "v": p.approval_view_id},
        separators=(",", ":"),
        sort_keys=True,
    )


def _reason_text(p: ApprovalPayload, severity: Severity, limit: int = 8) -> str:
    rows = [r for r in p.reasons if r.severity is severity]
    if not rows:
        return "none"
    out = []
    for r in rows[:limit]:
        detail = f" — {_clip(r.detail, 280)}" if r.detail else ""
        out.append(f"• `{_esc(r.source)}/{_esc(r.code)}`{detail}")
    if len(rows) > limit:
        out.append(f"• … +{len(rows) - limit} more")
    return "\n".join(out)


def _orderflow_text(p: ApprovalPayload, role: str, limit: int = 6) -> str:
    rows = [c for c in p.orderflow_evidence if c.role == role]
    if not rows:
        return "none"
    out = []
    for c in rows[:limit]:
        out.append(
            f"• `{_esc(c.name)}` {c.window_s}s → *{_esc(c.vote.value)}* — {_clip(c.detail, 360)}"
        )
    if len(rows) > limit:
        out.append(f"• … +{len(rows) - limit} more")
    return "\n".join(out)


def render_slack_message(
    p: ApprovalPayload,
    *,
    workflow_state: SlackWorkflowState = SlackWorkflowState.ACTIVE,
    banner: str | None = None,
) -> SlackMessage:
    """Render the exact approval view that the human is being shown.

    Slack has no disabled-button state.  Therefore ENTER is omitted whenever
    ``approval_allowed_now`` is false; REJECT remains available while the human
    workflow is open.
    """

    upp = p.units_per_point
    remaining = "-"
    if p.expires_at_ns is not None and p.safety_evaluated_wall_ns is not None:
        remaining = f"{max(0, (p.expires_at_ns - p.safety_evaluated_wall_ns) // 1_000_000_000)} s"

    open_workflow = workflow_state in (SlackWorkflowState.ACTIVE, SlackWorkflowState.HOLD)
    status_icon = {
        SlackWorkflowState.ACTIVE: "🟢",
        SlackWorkflowState.HOLD: "🟡",
        SlackWorkflowState.ENTER_RECORDED: "✅",
        SlackWorkflowState.REJECTED: "⛔",
        SlackWorkflowState.CLOSED: "🔒",
    }[workflow_state]

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Hermès · {p.symbol} {p.direction} · HUMAN_APPROVAL",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{status_icon} *{workflow_state.value}* · lifecycle `{_esc(p.status)}` · "
                    f"approval now *{'YES' if p.approval_allowed_now else 'NO'}*\n"
                    f"`setup {_short(p.setup_id)}` · `proposal {_short(p.proposal_id)}` · "
                    f"`view {_short(p.approval_view_id)}`"
                ),
            },
        },
    ]
    if banner:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{_esc(banner)}*"}}
        )

    blocks += [
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Entry*\n{_px(p.entry_reference, upp)} ({_esc(p.entry_side or '-')})"},
                {"type": "mrkdwn", "text": f"*Stop*\n{_px(p.proposed_stop, upp)}"},
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*Risk / contract*\n"
                        f"{'-' if p.risk_points is None else f'{p.risk_points:.2f} pt'} · "
                        f"{'-' if p.risk_usd_per_contract is None else f'${p.risk_usd_per_contract:.2f}'}"
                    ),
                },
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*Invalidation*\n{_px(p.structural_invalidation, upp)} "
                        f"({_esc(p.structure_source or '-')})"
                    ),
                },
                {"type": "mrkdwn", "text": f"*Expires*\n{remaining}"},
                {"type": "mrkdwn", "text": "*Take-profit*\nNONE"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*5m / 1m / 30s*\n"
                + "\n".join(_tf(t) for t in (p.regime_5m, p.setup_1m, p.trigger_30s)),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*PRIMARY order flow*\n" + _orderflow_text(p, "primary"),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Secondary / contextual order flow*\n" + _orderflow_text(p, "secondary"),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*BLOCKERS*\n" + _reason_text(p, Severity.BLOCK),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*TEMPORARY HOLDS*\n" + _reason_text(p, Severity.HOLD),
            },
        },
    ]

    cautions = _reason_text(p, Severity.CAUTION)
    supports = _reason_text(p, Severity.SUPPORT)
    if cautions != "none":
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*CAUTIONS*\n" + cautions}})
    if supports != "none":
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "*SUPPORTING EVIDENCE*\n" + supports}}
        )

    if p.notes:
        notes = "\n".join(f"• {_clip(n, 360)}" for n in p.notes[:6])
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "*IBKR MBP limitations*\n" + notes}}
        )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Hermès sends *no order*. ENTER is human intent only; fresh safety is re-checked.",
                }
            ],
        }
    )

    if open_workflow:
        elements = []
        if p.status == "ACTIONABLE" and p.approval_allowed_now:
            elements.append(
                {
                    "type": "button",
                    "action_id": ENTER_ACTION_ID,
                    "text": {"type": "plain_text", "text": "ENTER", "emoji": True},
                    "style": "primary",
                    "value": _action_value(p),
                }
            )
        elements.append(
            {
                "type": "button",
                "action_id": REJECT_ACTION_ID,
                "text": {"type": "plain_text", "text": "REJECT", "emoji": True},
                "style": "danger",
                "value": _action_value(p),
            }
        )
        blocks.append({"type": "actions", "elements": elements})

    fallback = (
        f"Hermès {p.symbol} {p.direction}: {workflow_state.value}; "
        f"status={p.status}; approval={'YES' if p.approval_allowed_now else 'NO'}; "
        "Hermès sends no order."
    )
    return SlackMessage(fallback, tuple(blocks))
