"""C9 human-approval payload: an immutable, self-contained view of one candidate for the future
HUMAN_APPROVAL workflow (e.g. a Slack message with ENTER / REJECT buttons in a later phase).

Building it sends nothing and executes nothing: there is no Slack call and no broker call here.
Prices are integer grid units plus exact point conversions; UNKNOWN volume stays separate; the
MBP limitations travel with the payload.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.decision.candidate import Assessment, OrderFlowComponent
from hermes.decision.lifecycle import CandidateRecord, CandidateStatus, StatusReason
from hermes.decision.safety import CANDIDATE_STATUS_NOT_ACTIONABLE, PURPOSE_APPROVAL, SafetyResult, approval_check
from hermes.market.bars import Bar

APPROVAL_SCHEMA_VERSION = 2

# Stable reason codes explaining why a payload is NOT approval-eligible (in addition to the bare
# SafetyPolicy codes of a supplied, failing approval-time SafetyResult).
SAFETY_NOT_EVALUATED = "safety_not_evaluated"                   # no fresh approval-time SafetyResult supplied
SAFETY_RESULT_NOT_FOR_APPROVAL = "safety_result_not_for_approval"
SAFETY_RESULT_OTHER_CANDIDATE = "safety_result_other_candidate"
SAFETY_RESULT_OUTDATED = "safety_result_outdated"               # judged an event-state older than the record
LIFECYCLE_HOLD_ACTIVE = "lifecycle_hold_active"
APPROVAL_GATE_CODES = frozenset({
    SAFETY_NOT_EVALUATED, SAFETY_RESULT_NOT_FOR_APPROVAL, SAFETY_RESULT_OTHER_CANDIDATE,
    SAFETY_RESULT_OUTDATED, LIFECYCLE_HOLD_ACTIVE, CANDIDATE_STATUS_NOT_ACTIONABLE})


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
    approval_allowed_now: bool         # lifecycle allows AND a fresh approval-time SafetyResult passed
    lifecycle_allows: bool             # ACTIONABLE and no temporary hold at the last lifecycle observation
    approval_denied_reasons: tuple[StatusReason, ...]   # empty iff approval_allowed_now
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
    approval_safety: SafetyResult | None = None   # the approval-time SafetyResult judged at build time


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


def approval_eligibility(rec: CandidateRecord, safety: SafetyResult | None
                         ) -> tuple[bool, tuple[StatusReason, ...]]:
    """Fail-closed: approval-eligible ONLY when the record is ACTIONABLE with no lifecycle hold AND a
    SafetyResult was supplied that (a) is a real SafetyResult, (b) was evaluated for approval,
    (c) for this very record, (d) on an event-state not older than the record's last observation,
    and (e) passed. The payload alone (static candidate information) can never make it eligible."""
    denied: list[StatusReason] = []
    if rec.status is not CandidateStatus.ACTIONABLE:
        denied.append(StatusReason(CANDIDATE_STATUS_NOT_ACTIONABLE, rec.status.value))
    elif not rec.approval_allowed_now:
        denied.append(StatusReason(LIFECYCLE_HOLD_ACTIVE,
                                   ",".join(h.code for h in rec.temporary_hold_reasons) or "not allowed"))
    if safety is None:
        denied.append(StatusReason(SAFETY_NOT_EVALUATED, "no approval-time SafetyResult supplied"))
    elif not isinstance(safety, SafetyResult):
        denied.append(StatusReason(SAFETY_NOT_EVALUATED, f"not a SafetyResult: {type(safety).__name__}"))
    else:
        if safety.purpose != PURPOSE_APPROVAL:
            denied.append(StatusReason(SAFETY_RESULT_NOT_FOR_APPROVAL, safety.purpose))
        if safety.subject != rec.setup_id:
            denied.append(StatusReason(SAFETY_RESULT_OTHER_CANDIDATE, f"{safety.subject} != {rec.setup_id}"))
        if safety.evaluated_wall_ns < rec.status_wall_ns:
            denied.append(StatusReason(SAFETY_RESULT_OUTDATED,
                                       f"evaluated_wall_ns={safety.evaluated_wall_ns} < {rec.status_wall_ns}"))
        if not safety.allowed:
            denied.extend(safety.hard_block_reasons)
            denied.extend(safety.temporary_hold_reasons)
    return not denied, tuple(denied)


def approval_payload(rec: CandidateRecord, safety: SafetyResult | None = None) -> ApprovalPayload:
    """``safety``: the approval-time ``SafetyResult`` (``safety.approval_check``) for the CURRENT
    event-state. Static candidate information is always reported; ``approval_allowed_now`` is True
    only per ``approval_eligibility`` (without a fresh passing SafetyResult it is always False with
    ``safety_not_evaluated``). Final statuses (NONE/BLOCKED/EXPIRED/STALE/INVALIDATED) are never eligible."""
    allowed, denied = approval_eligibility(rec, safety)
    c = rec.candidate
    r = c.risk
    tbar = c.trigger_30s.bar_context.latest if c.trigger_30s.bar_context is not None else None
    return ApprovalPayload(
        schema_version=APPROVAL_SCHEMA_VERSION, mode=c.mode, setup_id=rec.setup_id, status=rec.status.value,
        status_reasons=rec.reasons, actionable=rec.actionable,
        approval_allowed_now=allowed, lifecycle_allows=rec.approval_allowed_now, approval_denied_reasons=denied,
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
        approval_safety=safety,
    )


def current_approval_payload(engine, rec: CandidateRecord, cfg=None, now_wall_ns: int | None = None,
                             instrument_id: int | None = None) -> ApprovalPayload:
    """The preferred path: run the approval-time SafetyPolicy on the current event-state of the
    (read-only) engine, then build the payload from it. Sends nothing, executes nothing."""
    return approval_payload(rec, approval_check(engine, rec, cfg, now_wall_ns, instrument_id))
