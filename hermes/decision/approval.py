"""C9 human-approval payload: an immutable, self-contained view of one candidate for the future
HUMAN_APPROVAL workflow (e.g. a Slack message with ENTER / REJECT buttons in a later phase).

Building it sends nothing and executes nothing: there is no Slack call and no broker call here.
Prices are integer grid units plus exact point conversions; UNKNOWN volume stays separate; the
MBP limitations travel with the payload.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.decision.candidate import Assessment, OrderFlowComponent
from hermes.decision.lifecycle import CandidateRecord, StatusReason
from hermes.market.bars import Bar

APPROVAL_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class TimeframeSummary:
    stage: str                         # regime_5m | setup_1m | trigger_30s
    result: str
    window_end_s: int | None
    bars_used: int
    open: int | None
    high: int | None
    low: int | None
    close: int | None
    net_change_units: int | None
    buy_volume: int
    sell_volume: int
    unknown_volume: int                # never redistributed
    known_delta: int
    conditions: tuple[tuple[str, str, str], ...]   # (name, pass|fail|unavailable, detail)


@dataclass(frozen=True, slots=True)
class ApprovalPayload:
    schema_version: int
    mode: str                          # always HUMAN_APPROVAL in Phase C9
    setup_id: str
    status: str                        # lifecycle status at build time
    status_reasons: tuple[StatusReason, ...]
    actionable: bool                   # lifecycle status == ACTIONABLE
    approval_allowed_now: bool         # ACTIONABLE and no temporary hold at the last observation
    temporary_hold_reasons: tuple[StatusReason, ...]
    direction: str
    symbol: str
    instrument_id: int
    entry_reference: int | None        # LONG: best ask / SHORT: best bid at evaluation (never moved)
    entry_side: str | None
    proposed_stop: int | None
    structural_invalidation: int | None
    structure_source: str | None       # recent_1m_window_low / _high (rolling extreme, not a pivot)
    risk_points: float | None
    risk_usd_per_contract: float | None
    created_seq: int
    created_wall_ns: int
    trigger_bar_end_s: int | None
    expires_at_ns: int | None
    evaluation_lag_ms: int | None
    regime_5m: TimeframeSummary
    setup_1m: TimeframeSummary
    trigger_30s: TimeframeSummary
    orderflow_evidence: tuple[OrderFlowComponent, ...]
    supporting_reasons: tuple[str, ...]
    caution_reasons: tuple[str, ...]
    blocking_reasons: tuple[str, ...]
    session_rth: bool
    market_data_ok: bool
    continuity: tuple[tuple[str, int | None], ...]
    notes: tuple[str, ...]             # MBP epistemic limitations
    annotations: tuple[str, ...]       # informational only (future memory/shadow/LLM); never gates
    screenshot_ref: str | None = None  # reserved for a later phase


def _summary(a: Assessment, trigger_bar: Bar | None = None) -> TimeframeSummary:
    bc = a.bar_context
    if trigger_bar is not None:
        b = trigger_bar
        return TimeframeSummary(a.stage, a.result, b.end_s, 1, b.open, b.high, b.low, b.close,
                                b.close - b.open if b.trades else None, b.buy_volume, b.sell_volume,
                                b.unknown_volume, b.known_delta,
                                tuple((c.name, c.status, c.detail) for c in a.conditions))
    return TimeframeSummary(
        a.stage, a.result, bc.latest.end_s if bc is not None and bc.latest is not None else None,
        bc.bars_used if bc else 0, bc.open if bc else None, bc.high if bc else None, bc.low if bc else None,
        bc.close if bc else None, bc.net_change_units if bc else None, bc.buy_volume if bc else 0,
        bc.sell_volume if bc else 0, bc.unknown_volume if bc else 0, bc.known_delta if bc else 0,
        tuple((c.name, c.status, c.detail) for c in a.conditions))


def approval_payload(rec: CandidateRecord) -> ApprovalPayload:
    c = rec.candidate
    r = c.risk
    tbar = c.trigger_30s.bar_context.latest if c.trigger_30s.bar_context is not None else None
    return ApprovalPayload(
        schema_version=APPROVAL_SCHEMA_VERSION, mode=c.mode, setup_id=rec.setup_id, status=rec.status.value,
        status_reasons=rec.reasons, actionable=rec.actionable, approval_allowed_now=rec.approval_allowed_now,
        temporary_hold_reasons=rec.temporary_hold_reasons, direction=c.direction.value, symbol=c.symbol,
        instrument_id=c.instrument_id, entry_reference=c.entry_reference,
        entry_side=r.entry_side if r is not None and c.entry_reference is not None else None,
        proposed_stop=c.proposed_stop, structural_invalidation=c.structural_invalidation,
        structure_source=r.structure_source if r is not None and c.entry_reference is not None else None,
        risk_points=c.risk_points,
        risk_usd_per_contract=r.risk_usd_per_contract if r is not None and c.entry_reference is not None else None,
        created_seq=rec.created_seq, created_wall_ns=rec.created_wall_ns, trigger_bar_end_s=c.trigger_bar_end_s,
        expires_at_ns=rec.expires_at_ns, evaluation_lag_ms=c.evaluation_lag_ms,
        regime_5m=_summary(c.regime_5m), setup_1m=_summary(c.setup_1m), trigger_30s=_summary(c.trigger_30s, tbar),
        orderflow_evidence=c.orderflow_evidence, supporting_reasons=c.supporting_reasons,
        caution_reasons=c.caution_reasons, blocking_reasons=c.blocking_reasons, session_rth=c.session_rth,
        market_data_ok=c.market_data_ok, continuity=c.continuity, notes=c.notes, annotations=rec.annotations,
    )
