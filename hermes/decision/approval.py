"""C9 human-approval payload: an immutable, self-contained view of one candidate for the future
HUMAN_APPROVAL workflow (e.g. a Slack message with ENTER / REJECT buttons in a later phase).

Building it sends nothing and executes nothing: there is no Slack call and no broker call here.
Prices are integer grid units plus exact point conversions; UNKNOWN volume stays separate; the
MBP limitations travel with the payload.

C9e: every reason in the payload has ONE structured shape (``reasons.Reason``: source, code,
detail, severity) and two deterministic identities:

* ``proposal_id``      the immutable trade PROPOSAL (setup, direction, entry, invalidation, stop,
                       risk, creation-time context and evidence). It never changes because time
                       advanced, the lifecycle status changed or safety was re-observed; it changes
                       iff the proposal itself changes.
* ``approval_view_id`` the exact VIEW shown at one moment (proposal_id + lifecycle status +
                       approval_allowed_now + approval-time SafetyResult + holds/blocks). It changes
                       whenever that view changes.

A future human response references both (``hermes.decision.response``). Even an approved,
previously-safe view is never trusted later: execution (not part of C9) would re-run a fresh
SafetyPolicy. Approval eligibility is fail-closed (C9d). Canonical JSON-able form and a
deterministic plain-text rendering are provided.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from enum import Enum

from hermes.decision.candidate import Assessment, OrderFlowComponent
from hermes.decision.lifecycle import CandidateRecord, CandidateStatus
from hermes.decision.reasons import SRC_APPROVAL, SRC_LIFECYCLE, Reason, Severity, codes, ordered

AFTER_CREATION_SOURCES = frozenset({SRC_APPROVAL, SRC_LIFECYCLE})
from hermes.decision.safety import (
    CANDIDATE_STATUS_NOT_ACTIONABLE, HOLD_CODES, PURPOSE_APPROVAL, SafetyResult, approval_check)
from hermes.market.bars import Bar
from hermes.replay.fingerprint import digest

APPROVAL_SCHEMA_VERSION = 3

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
    proposal_id: str                   # immutable proposal identity ("P" + 23 hex)
    approval_view_id: str              # identity of this exact approval view ("V" + 23 hex)
    setup_id: str
    status: str                        # lifecycle status at build time
    actionable: bool                   # lifecycle status == ACTIONABLE
    approval_allowed_now: bool         # lifecycle allows AND a fresh approval-time SafetyResult passed
    lifecycle_allows: bool             # ACTIONABLE and no temporary hold at the last lifecycle observation
    safety_evaluated_wall_ns: int | None   # event time of the approval-time SafetyResult (None: not evaluated)
    direction: str
    symbol: str
    instrument_id: int
    units_per_point: int | None        # grid units per index point (MNQ: 4)
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
    reasons: tuple[Reason, ...]        # ONE structured list, ordered BLOCK > HOLD > CAUTION > SUPPORT > INFO
    session_rth: bool
    market_data_ok: bool
    continuity: tuple[tuple[str, int | None], ...]
    notes: tuple[str, ...]             # MBP epistemic limitations
    annotations: tuple[str, ...]       # informational only (future memory/shadow/LLM); never gates
    screenshot_ref: str | None = None  # reserved for a later phase
    approval_safety: SafetyResult | None = None   # the approval-time SafetyResult judged at build time

    @property
    def denied_reasons(self) -> tuple[Reason, ...]:
        """Why approval is not allowed now (empty iff ``approval_allowed_now``)."""
        return tuple(r for r in self.reasons if r.source == SRC_APPROVAL
                     and r.severity in (Severity.BLOCK, Severity.HOLD))

    def codes(self, *, severity: Severity | None = None, source: str | None = None) -> tuple[str, ...]:
        return codes(self.reasons, severity=severity, source=source)


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


def approval_eligibility(rec: CandidateRecord, safety: SafetyResult | None) -> tuple[bool, tuple[Reason, ...]]:
    """Fail-closed: approval-eligible ONLY when the record is ACTIONABLE with no lifecycle hold AND a
    SafetyResult was supplied that (a) is a real SafetyResult, (b) was evaluated for approval,
    (c) for this very record, (d) on an event-state not older than the record's last observation,
    and (e) passed. The payload alone (static candidate information) can never make it eligible."""
    denied: list[Reason] = []

    def deny(code: str, detail: str, sev: Severity = Severity.BLOCK) -> None:
        denied.append(Reason(SRC_APPROVAL, code, detail, sev))

    if rec.status is not CandidateStatus.ACTIONABLE:
        deny(CANDIDATE_STATUS_NOT_ACTIONABLE, rec.status.value)
    elif not rec.approval_allowed_now:
        deny(LIFECYCLE_HOLD_ACTIVE, ",".join(h.code for h in rec.temporary_hold_reasons) or "not allowed",
             Severity.HOLD)
    if safety is None:
        deny(SAFETY_NOT_EVALUATED, "no approval-time SafetyResult supplied")
    elif not isinstance(safety, SafetyResult):
        deny(SAFETY_NOT_EVALUATED, f"not a SafetyResult: {type(safety).__name__}")
    else:
        if safety.purpose != PURPOSE_APPROVAL:
            deny(SAFETY_RESULT_NOT_FOR_APPROVAL, safety.purpose)
        if safety.subject != rec.setup_id:
            deny(SAFETY_RESULT_OTHER_CANDIDATE, f"{safety.subject} != {rec.setup_id}")
        if safety.evaluated_wall_ns < rec.status_wall_ns:
            deny(SAFETY_RESULT_OUTDATED, f"evaluated_wall_ns={safety.evaluated_wall_ns} < {rec.status_wall_ns}")
        if not safety.allowed:
            for r in safety.hard_block_reasons:
                deny(r.code, r.detail)
            for r in safety.temporary_hold_reasons:
                deny(r.code, r.detail, Severity.HOLD if r.code in HOLD_CODES else Severity.BLOCK)
    return not denied, tuple(dict.fromkeys(denied))


def lifecycle_reasons(rec: CandidateRecord) -> tuple[Reason, ...]:
    """Reasons produced AFTER creation (the creation evidence is ``candidate.reasons``)."""
    out: list[Reason] = []
    if rec.transitions:
        for r in rec.transitions[-1].reasons:
            out.append(Reason(SRC_LIFECYCLE, r.code, r.detail, Severity.BLOCK))
    for r in rec.temporary_hold_reasons:
        out.append(Reason(SRC_LIFECYCLE, r.code, r.detail, Severity.HOLD))
    if rec.status is CandidateStatus.ACTIONABLE:
        out.append(Reason(SRC_LIFECYCLE, "actionable_proposal", "HUMAN_APPROVAL only", Severity.INFO))
    return tuple(out)


def approval_payload(rec: CandidateRecord, safety: SafetyResult | None = None) -> ApprovalPayload:
    """``safety``: the approval-time ``SafetyResult`` (``safety.approval_check``) for the CURRENT
    event-state. Static candidate information is always reported; ``approval_allowed_now`` is True
    only per ``approval_eligibility`` (without a fresh passing SafetyResult it is always False with
    ``safety_not_evaluated``). Final statuses (NONE/BLOCKED/EXPIRED/STALE/INVALIDATED) are never eligible."""
    allowed, denied = approval_eligibility(rec, safety)
    c = rec.candidate
    r = c.risk
    act = r is not None and c.entry_reference is not None
    tbar = c.trigger_30s.bar_context.latest if c.trigger_30s.bar_context is not None else None
    p = ApprovalPayload(
        schema_version=APPROVAL_SCHEMA_VERSION, mode=c.mode, proposal_id="", approval_view_id="",
        setup_id=rec.setup_id,
        status=rec.status.value, actionable=rec.actionable, approval_allowed_now=allowed,
        lifecycle_allows=rec.approval_allowed_now,
        safety_evaluated_wall_ns=safety.evaluated_wall_ns if isinstance(safety, SafetyResult) else None,
        direction=c.direction.value, symbol=c.symbol, instrument_id=c.instrument_id,
        units_per_point=c.units_per_point, entry_reference=c.entry_reference,
        entry_side=r.entry_side if act else None,
        proposed_stop=c.proposed_stop, structural_invalidation=c.structural_invalidation,
        structure_source=r.structure_source if act else None,
        risk_points=c.risk_points, risk_usd_per_contract=r.risk_usd_per_contract if act else None,
        created_seq=rec.created_seq, created_wall_ns=rec.created_wall_ns, trigger_bar_end_s=c.trigger_bar_end_s,
        expires_at_ns=rec.expires_at_ns, evaluation_lag_ms=c.evaluation_lag_ms,
        regime_5m=_summary(c.regime_5m), setup_1m=_summary(c.setup_1m), trigger_30s=_summary(c.trigger_30s, tbar),
        orderflow_evidence=c.orderflow_evidence,
        reasons=ordered(denied + lifecycle_reasons(rec) + c.reasons),
        session_rth=c.session_rth, market_data_ok=c.market_data_ok, continuity=c.continuity, notes=c.notes,
        annotations=rec.annotations, approval_safety=safety if isinstance(safety, SafetyResult) else None,
    )
    p = replace(p, proposal_id=compute_proposal_id(p))
    return replace(p, approval_view_id=compute_approval_view_id(p))


def current_approval_payload(engine, rec: CandidateRecord, cfg=None, now_wall_ns: int | None = None,
                             instrument_id: int | None = None) -> ApprovalPayload:
    """The preferred path: run the approval-time SafetyPolicy on the current event-state of the
    (read-only) engine, then build the payload from it. Sends nothing, executes nothing."""
    return approval_payload(rec, approval_check(engine, rec, cfg, now_wall_ns, instrument_id))


# ---------------------------------------------------------------------------- canonical form

def _plain(obj):
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (tuple, list)):
        return [_plain(x) for x in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return {f.name: _plain(getattr(obj, f.name)) for f in fields(obj)}
    raise TypeError(f"not serializable: {type(obj).__name__}")


def payload_to_dict(p: ApprovalPayload) -> dict:
    """Canonical JSON-able form (plain dict / list / str / int / float / bool / None)."""
    return _plain(p)


# Immutable proposal facts (all fixed at candidate creation). NOT included: status, actionable,
# approval_allowed_now, lifecycle_allows, safety_evaluated_wall_ns, approval_safety, annotations,
# lifecycle/approval reasons, screenshot_ref, approval_view_id.
PROPOSAL_FIELDS = (
    "schema_version", "mode", "setup_id", "direction", "symbol", "instrument_id", "units_per_point",
    "entry_reference", "entry_side", "proposed_stop", "structural_invalidation", "structure_source",
    "risk_points", "risk_usd_per_contract", "created_seq", "created_wall_ns", "trigger_bar_end_s",
    "expires_at_ns", "evaluation_lag_ms", "regime_5m", "setup_1m", "trigger_30s", "orderflow_evidence",
    "session_rth", "market_data_ok", "continuity", "notes",
)


def proposal_facts(p: ApprovalPayload) -> dict:
    d = {name: _plain(getattr(p, name)) for name in PROPOSAL_FIELDS}
    d["creation_reasons"] = _plain(tuple(r for r in p.reasons if r.source not in AFTER_CREATION_SOURCES))
    return d


def compute_proposal_id(p: ApprovalPayload) -> str:
    return "P" + digest(("hermes-proposal-v1", proposal_facts(p)))[:23]


def compute_approval_view_id(p: ApprovalPayload) -> str:
    """Everything shown (incl. proposal_id, status, approval_allowed_now, the approval-time
    SafetyResult and every reason) except the view id itself."""
    d = _plain(replace(p, approval_view_id=""))
    return "V" + digest(("hermes-approval-view-v1", d))[:23]


def verify_proposal_id(p: ApprovalPayload) -> bool:
    """True iff ``proposal_id`` matches the proposal facts in this payload."""
    return p.proposal_id == compute_proposal_id(p)


def verify_approval_view_id(p: ApprovalPayload) -> bool:
    """True iff both ids match this payload's content (tamper / version check)."""
    return verify_proposal_id(p) and p.approval_view_id == compute_approval_view_id(p)


# ---------------------------------------------------------------------------- rendering

def _px(units: int | None, upp: int | None) -> str:
    if units is None:
        return "-"
    if not upp:
        return f"{units} units"
    return f"{units / upp:.2f}"


def _hms(ns_or_s: int | None, *, seconds: bool = False) -> str:
    if ns_or_s is None:
        return "-"
    s = ns_or_s if seconds else ns_or_s // 1_000_000_000
    d = s % 86_400
    return f"{d // 3600:02d}:{d % 3600 // 60:02d}:{d % 60:02d} UTC"


def _tf(t: TimeframeSummary) -> str:
    net = "-" if t.net_change_units is None else f"{t.net_change_units:+d} ticks"
    return (f"{t.stage:<11} {t.result:<7} bars={t.bars_used} net={net} known_delta={t.known_delta:+d} "
            f"B/S/U={t.buy_volume}/{t.sell_volume}/{t.unknown_volume}")


_SECTIONS = ((Severity.BLOCK, "BLOCKERS"), (Severity.HOLD, "TEMPORARY HOLDS"), (Severity.CAUTION, "CAUTIONS"),
             (Severity.SUPPORT, "SUPPORTING EVIDENCE"), (Severity.INFO, "INFO"))


def render_approval_text(p: ApprovalPayload) -> str:
    """Deterministic plain text for a human reviewer. Pure function of the payload; sends nothing.
    No global confidence score is shown (none exists)."""
    upp = p.units_per_point
    L = [f"HERMES C9 - {p.mode} PROPOSAL - Hermès sends no order",
         f"{p.symbol} {p.direction}",
         f"Lifecycle status      : {p.status}",
         f"Approval allowed now  : {'YES' if p.approval_allowed_now else 'NO'}"
         + ("" if p.safety_evaluated_wall_ns is not None else " (approval-time safety not evaluated)"),
         f"setup_id              : {p.setup_id}",
         f"proposal_id           : {p.proposal_id}",
         f"approval_view_id      : {p.approval_view_id}"]
    if p.entry_reference is not None:
        L += [f"Entry reference       : {_px(p.entry_reference, upp)} ({p.entry_side} at evaluation; never moved)",
              f"Proposed stop         : {_px(p.proposed_stop, upp)}",
              f"Risk                  : {p.risk_points:.2f} pt = ${p.risk_usd_per_contract:.2f} per contract",
              f"Structural invalidation: {_px(p.structural_invalidation, upp)} ({p.structure_source} minus buffer)",
              "Take-profit           : NONE (by design)"]
    remaining = ""
    if p.expires_at_ns is not None and p.safety_evaluated_wall_ns is not None:
        left = max(0, (p.expires_at_ns - p.safety_evaluated_wall_ns) // 1_000_000_000)
        remaining = f" ({left} s remaining at view time)"
    L.append(f"Trigger bar end       : {_hms(p.trigger_bar_end_s, seconds=True)} (evaluated +{p.evaluation_lag_ms} ms)")
    L.append(f"Expires               : {_hms(p.expires_at_ns)}{remaining}")
    for sev, title in _SECTIONS:
        rs = [r for r in p.reasons if r.severity is sev]
        if not rs and sev not in (Severity.BLOCK, Severity.HOLD):
            continue
        L.append(f"{title}:")
        L += [f"  {r.source}/{r.code}" + (f": {r.detail}" if r.detail else "") for r in rs] or ["  none"]
    L.append("CONTEXT (5m regime / 1m setup / 30s trigger):")
    L += ["  " + _tf(t) for t in (p.regime_5m, p.setup_1m, p.trigger_30s)]
    if p.orderflow_evidence:
        L.append("ORDER FLOW (each component is one vote; UNKNOWN never votes):")
        L += [f"  {c.name} ({c.role}, {c.window_s}s): {c.vote.value} - {c.detail}" for c in p.orderflow_evidence]
    if p.notes:
        L.append("MARKET-DATA LIMITS (IBKR MBP):")
        L += [f"  - {n}" for n in p.notes]
    return "\n".join(L) + "\n"
