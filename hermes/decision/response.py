"""C9e — PURE, immutable human response to an approval view (ENTER / REJECT).

This is NOT an execution path. Creating a ``HumanApprovalResponse`` sends nothing, changes no
lifecycle state and cannot bypass the SafetyPolicy. ENTER is recorded human INTENT only; it is
never, by itself, an execution authorization (``authorizes_execution`` is always False).

A response references both identities of what the human saw:

* ``proposal_id``      which immutable proposal the human decided on;
* ``approval_view_id`` which exact view (status + approval-time safety) was shown.

A previously-safe view is never trusted later. ``execution_prerequisites`` documents, as a pure
function, everything a FUTURE execution layer would need before a broker action could even be
considered — human ENTER + the same proposal still ACTIONABLE + a FRESH passing approval-time
SafetyPolicy result (not older than the response) + a RiskEligibilityPolicy (not implemented) + an
enabled execution layer (does not exist in Phase C). In Phase C9 it can therefore never be satisfied.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from hermes.decision.approval import approval_payload, verify_approval_view_id
from hermes.decision.lifecycle import CandidateRecord, CandidateStatus
from hermes.decision.reasons import SRC_RESPONSE, Reason, Severity
from hermes.decision.safety import HOLD_CODES, PURPOSE_APPROVAL, SafetyResult

RESPONSE_SCHEMA_VERSION = 1
MAX_NOTE_CHARS = 500
_HEX = frozenset("0123456789abcdef")


class HumanAction(str, Enum):
    ENTER = "ENTER"     # human intent to enter (NOT an execution authorization)
    REJECT = "REJECT"


def _is_id(value: object, prefix: str) -> bool:
    return isinstance(value, str) and len(value) == 24 and value[0] == prefix and set(value[1:]) <= _HEX


@dataclass(frozen=True, slots=True)
class HumanApprovalResponse:
    action: HumanAction
    setup_id: str
    proposal_id: str
    approval_view_id: str
    response_wall_ns: int | None = None     # event time of the response, where available
    response_seq: int | None = None
    note: str = ""                          # optional human note (informational only)
    schema_version: int = RESPONSE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.action, HumanAction):
            raise ValueError(f"action must be a HumanAction: {self.action!r}")
        if not _is_id(self.setup_id, "S"):
            raise ValueError(f"invalid setup_id {self.setup_id!r}")
        if not _is_id(self.proposal_id, "P"):
            raise ValueError(f"invalid proposal_id {self.proposal_id!r}")
        if not _is_id(self.approval_view_id, "V"):
            raise ValueError(f"invalid approval_view_id {self.approval_view_id!r}")
        for name in ("response_wall_ns", "response_seq"):
            v = getattr(self, name)
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
                raise ValueError(f"{name} must be a non-negative int or None")
        if not isinstance(self.note, str) or len(self.note) > MAX_NOTE_CHARS:
            raise ValueError(f"note must be a string of at most {MAX_NOTE_CHARS} characters")

    @property
    def authorizes_execution(self) -> bool:
        """Always False: a human response is intent, never an execution authorization by itself."""
        return False


def _r(code: str, detail: str, sev: Severity = Severity.BLOCK) -> Reason:
    return Reason(SRC_RESPONSE, code, detail, sev)


def response_matches_view(resp: HumanApprovalResponse, view) -> tuple[bool, tuple[Reason, ...]]:
    """Pure consistency check of a response against the ApprovalPayload that was shown."""
    out: list[Reason] = []
    if resp.setup_id != view.setup_id:
        out.append(_r("response_setup_mismatch", f"{resp.setup_id} != {view.setup_id}"))
    if resp.proposal_id != view.proposal_id:
        out.append(_r("response_proposal_mismatch", f"{resp.proposal_id} != {view.proposal_id}"))
    if resp.approval_view_id != view.approval_view_id:
        out.append(_r("response_view_mismatch", f"{resp.approval_view_id} != {view.approval_view_id}"))
    if not verify_approval_view_id(view):
        out.append(_r("approval_view_id_invalid", "view content does not match its ids"))
    if (resp.response_wall_ns is not None and view.safety_evaluated_wall_ns is not None
            and resp.response_wall_ns < view.safety_evaluated_wall_ns):
        out.append(_r("response_precedes_view", f"{resp.response_wall_ns} < {view.safety_evaluated_wall_ns}"))
    if resp.action is HumanAction.ENTER and not view.approval_allowed_now:
        out.append(_r("enter_on_non_approvable_view", "the view shown was not approval-eligible"))
    return not out, tuple(out)


@dataclass(frozen=True, slots=True)
class ExecutionPrerequisites:
    satisfied: bool                   # always False in Phase C9 (no RiskEligibilityPolicy, no execution layer)
    reasons: tuple[Reason, ...]

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(r.code for r in self.reasons)


def execution_prerequisites(resp: HumanApprovalResponse, rec: CandidateRecord,
                            fresh_safety: SafetyResult | None) -> ExecutionPrerequisites:
    """Pure documentation-as-code of what a FUTURE execution layer would require. Never trusts an old
    ``approval_allowed_now``: the SafetyResult must be a fresh approval-time check of THIS record,
    not older than the response nor the record's last observation, and it must pass. Sends nothing."""
    out: list[Reason] = []
    if resp.action is not HumanAction.ENTER:
        out.append(_r("human_enter_missing", resp.action.value))
    if resp.setup_id != rec.setup_id:
        out.append(_r("response_setup_mismatch", f"{resp.setup_id} != {rec.setup_id}"))
    current = approval_payload(rec)
    if resp.proposal_id != current.proposal_id:
        out.append(_r("proposal_changed", f"{resp.proposal_id} != {current.proposal_id}"))
    if rec.status is not CandidateStatus.ACTIONABLE:
        out.append(_r("proposal_not_valid", rec.status.value))
    if fresh_safety is None or not isinstance(fresh_safety, SafetyResult):
        out.append(_r("fresh_safety_missing", "a fresh approval-time SafetyResult is required"))
    else:
        if fresh_safety.purpose != PURPOSE_APPROVAL or fresh_safety.subject != rec.setup_id:
            out.append(_r("fresh_safety_mismatch", f"{fresh_safety.purpose}/{fresh_safety.subject}"))
        floor = max(rec.status_wall_ns, resp.response_wall_ns or 0)
        if fresh_safety.evaluated_wall_ns < floor:
            out.append(_r("fresh_safety_outdated", f"evaluated_wall_ns={fresh_safety.evaluated_wall_ns} < {floor}"))
        if not fresh_safety.allowed:
            out.extend(_r(x.code, x.detail) for x in fresh_safety.hard_block_reasons)
            out.extend(_r(x.code, x.detail, Severity.HOLD if x.code in HOLD_CODES else Severity.BLOCK)
                       for x in fresh_safety.temporary_hold_reasons)
    out.append(_r("risk_eligibility_policy_not_implemented", "future additive RiskEligibilityPolicy (e.g. max trades/day)"))
    out.append(_r("execution_layer_disabled", "Phase C has no execution layer; HUMAN_APPROVAL proposals only"))
    return ExecutionPrerequisites(False, tuple(dict.fromkeys(out)))
