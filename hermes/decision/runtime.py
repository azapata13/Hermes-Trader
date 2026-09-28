"""C9f — the ONE decision runtime used identically by the live pipeline and by replay.

    MarketEngine (read-only) -> CandidateDriver (DecisionContext -> SetupCandidate -> lifecycle
    -> SafetyPolicy) -> ApprovalPayload (at ACTIONABLE creation) -> journal + decision checkpoints

``DecisionRuntime.after_event(raw, events, now)`` is a pipeline consumer / replay observer. Per raw
event it is O(1) unless a 30 s bar completed (one evaluation) or a candidate is ACTIONABLE (one
lifecycle re-check). It never mutates market state, uses event time only, sends nothing and has no
order path. Human responses remain data (``hermes.decision.response``).

Decision journal (append-only, compact, deterministic): one record per decision TRANSITION, never
per tick. Decision checkpoints: ``(seq, kinds, decision_fingerprint)`` after every event that
produced journal records, plus a final one. They are kept SEPARATE from the market checkpoints /
market state hash (HASH_VERSION is unaffected).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from hermes.config import DecisionConfig
from hermes.decision.approval import ApprovalPayload, current_approval_payload
from hermes.decision.candidate import CandidateEngine, SetupCandidate
from hermes.decision.driver import CandidateDriver
from hermes.decision.lifecycle import CandidateRecord, CandidateStatus, StatusReason
from hermes.decision.reasons import SRC_LIFECYCLE, Reason, Severity
from hermes.replay.fingerprint import digest

JOURNAL_SCHEMA_VERSION = 1


class JournalKind(str, Enum):
    DECISION_EVALUATED = "DECISION_EVALUATED"
    CANDIDATE_ACTIONABLE = "CANDIDATE_ACTIONABLE"
    APPROVAL_VIEW_CREATED = "APPROVAL_VIEW_CREATED"
    TEMPORARY_HOLD_ENTERED = "TEMPORARY_HOLD_ENTERED"
    TEMPORARY_HOLD_CLEARED = "TEMPORARY_HOLD_CLEARED"
    CANDIDATE_BLOCKED = "CANDIDATE_BLOCKED"
    CANDIDATE_STALE = "CANDIDATE_STALE"
    CANDIDATE_INVALIDATED = "CANDIDATE_INVALIDATED"
    CANDIDATE_EXPIRED = "CANDIDATE_EXPIRED"


_TRANSITION_KIND = {
    CandidateStatus.BLOCKED: JournalKind.CANDIDATE_BLOCKED,
    CandidateStatus.STALE: JournalKind.CANDIDATE_STALE,
    CandidateStatus.INVALIDATED: JournalKind.CANDIDATE_INVALIDATED,
    CandidateStatus.EXPIRED: JournalKind.CANDIDATE_EXPIRED,
}

ReasonCode = tuple[str, str, str]          # (severity, source, code) — compact, no free-text detail


@dataclass(frozen=True, slots=True)
class JournalRecord:
    kind: JournalKind
    seq: int
    wall_ns: int
    instrument_id: int | None
    setup_id: str | None
    proposal_id: str | None
    approval_view_id: str | None
    status: str | None
    direction: str | None
    approval_allowed_now: bool | None
    reasons: tuple[ReasonCode, ...]
    decision_fingerprint: str            # driver fingerprint AFTER this event (same for records of one event)

    def row(self) -> list:
        """Compact JSON-able row (stable column order)."""
        return [self.kind.value, self.seq, self.wall_ns, self.instrument_id, self.setup_id, self.proposal_id,
                self.approval_view_id, self.status, self.direction, self.approval_allowed_now,
                [list(r) for r in self.reasons], self.decision_fingerprint]

    @classmethod
    def from_row(cls, r: list) -> "JournalRecord":
        return cls(JournalKind(r[0]), r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9],
                   tuple(tuple(x) for x in r[10]), r[11])


@dataclass(frozen=True, slots=True)
class DecisionCheckpoint:
    seq: int
    kind: str                            # journal kinds of that event joined with "+", or "final"
    fingerprint: str

    def row(self) -> list:
        return [self.seq, self.kind, self.fingerprint]


def _codes(reasons) -> tuple[ReasonCode, ...]:
    return tuple((r.severity.value, r.source, r.code) for r in reasons)


def _status_codes(reasons: tuple[StatusReason, ...], sev: Severity) -> tuple[ReasonCode, ...]:
    return _codes(Reason(SRC_LIFECYCLE, r.code, r.detail, sev) for r in reasons)


class DecisionRuntime:
    """Live pipeline consumer AND replay observer (identical code path)."""

    def __init__(self, engine, cfg: DecisionConfig | None = None, instrument_id: int | None = None,
                 on_record=None, max_records: int = 500_000) -> None:
        self.engine = engine
        self.cfg = cfg or DecisionConfig()
        self.driver = CandidateDriver(engine, CandidateEngine(self.cfg, instrument_id))
        self.on_record = on_record            # optional read-only hook (live logging); never gates anything
        self.max_records = max_records
        self.journal: list[JournalRecord] = []
        self.checkpoints: list[DecisionCheckpoint] = []
        self.final: DecisionCheckpoint | None = None
        self.proposals: dict[str, str] = {}   # setup_id -> proposal_id (ACTIONABLE candidates)
        self.last_view: ApprovalPayload | None = None
        self.dropped = 0
        self.last_seq = 0
        self.raw_count = 0
        self._chain = "0" * 64                # rolling digest over every journal row ever produced

    # ------------------------------------------------------------------ consumer interface
    def after_event(self, raw, events, now_mono_ns: int) -> None:
        self.raw_count += 1
        self.last_seq = raw.seq
        lc = self.driver.lifecycle
        before = ({sid: rec.temporary_hold_reasons for sid, rec in lc.active.items()} if lc.active else None)
        cand = self.driver.after_event(raw, events, now_mono_ns)
        if before is None and cand is None:
            return                            # the common case: nothing active, no bar completed
        out: list[tuple] = []
        if before is not None:
            self._lifecycle_records(before, raw, out)
        if cand is not None:
            self._evaluation_records(cand, raw, out)
        if out:
            self._emit(out, raw)

    def _lifecycle_records(self, before: dict, raw, out: list) -> None:
        lc = self.driver.lifecycle
        for t in self.driver.last_transitions:
            out.append((_TRANSITION_KIND[t.to_status], t.setup_id, self.proposals.get(t.setup_id), None,
                        t.to_status.value, None, False, _status_codes(t.reasons, Severity.BLOCK)))
        for sid, old_hold in before.items():
            rec = lc.active.get(sid)
            if rec is None or rec.temporary_hold_reasons == old_hold:
                continue
            if rec.temporary_hold_reasons and not old_hold:
                kind = JournalKind.TEMPORARY_HOLD_ENTERED
            elif old_hold and not rec.temporary_hold_reasons:
                kind = JournalKind.TEMPORARY_HOLD_CLEARED
            else:
                kind = JournalKind.TEMPORARY_HOLD_ENTERED   # hold reason changed while held
            out.append((kind, sid, self.proposals.get(sid), None, rec.status.value, None,
                        rec.approval_allowed_now, _status_codes(rec.temporary_hold_reasons, Severity.HOLD)))

    def _evaluation_records(self, cand: SetupCandidate, raw, out: list) -> None:
        rec: CandidateRecord | None = self.driver.lifecycle.latest()
        if rec is None or rec.candidate is not cand:
            return
        codes = _codes(cand.reasons)
        out.append((JournalKind.DECISION_EVALUATED, rec.setup_id, None, None, rec.status.value,
                    cand.direction.value, rec.approval_allowed_now, codes))
        if rec.status is CandidateStatus.ACTIONABLE:
            view = current_approval_payload(self.engine, rec, self.cfg, now_wall_ns=raw.recv_wall_ns,
                                            instrument_id=cand.instrument_id)
            self.proposals[rec.setup_id] = view.proposal_id
            self.last_view = view
            out.append((JournalKind.CANDIDATE_ACTIONABLE, rec.setup_id, view.proposal_id, None, rec.status.value,
                        cand.direction.value, rec.approval_allowed_now, ()))
            out.append((JournalKind.APPROVAL_VIEW_CREATED, rec.setup_id, view.proposal_id, view.approval_view_id,
                        view.status, cand.direction.value, view.approval_allowed_now, _codes(view.denied_reasons)))
        elif rec.status is CandidateStatus.BLOCKED:
            out.append((JournalKind.CANDIDATE_BLOCKED, rec.setup_id, None, None, rec.status.value,
                        cand.direction.value, False, tuple(c for c in codes if c[0] in ("BLOCK", "HOLD"))))

    def _emit(self, out: list, raw) -> None:
        fp = self.driver.fingerprint()
        iid = self._instrument_id()
        kinds = []
        for kind, sid, pid, vid, status, direction, allowed, codes in out:
            r = JournalRecord(kind, raw.seq, raw.recv_wall_ns, iid, sid, pid, vid, status, direction, allowed,
                              codes, fp)
            kinds.append(kind.value)
            self._chain = digest((self._chain, r.row()))
            if len(self.journal) < self.max_records:
                self.journal.append(r)
            else:
                self.dropped += 1
            if self.on_record is not None:
                self.on_record(r, self.last_view if kind is JournalKind.APPROVAL_VIEW_CREATED else None)
        if len(self.checkpoints) < self.max_records:
            self.checkpoints.append(DecisionCheckpoint(raw.seq, "+".join(kinds), self.fingerprint()))
        else:
            self.dropped += 1

    def _instrument_id(self) -> int | None:
        iid = self.driver.candidates.instrument_id
        if iid is not None:
            return iid
        insts = self.engine.instruments
        return next(iter(insts)) if insts else None

    # ------------------------------------------------------------------ results
    def fingerprint(self) -> str:
        """Decision-layer fingerprint: candidate + lifecycle state and every journal row so far.
        Independent of (and never folded into) the market state hash."""
        return digest(("hermes-decision-runtime", JOURNAL_SCHEMA_VERSION, self.driver.fingerprint(), self._chain,
                       len(self.journal) + self.dropped))

    def finalize(self) -> DecisionCheckpoint:
        self.final = DecisionCheckpoint(self.last_seq, "final", self.fingerprint())
        return self.final

    def counts(self) -> dict:
        lc = self.driver.lifecycle
        st = self.driver.candidates.stats
        by_kind: dict[str, int] = {}
        for r in self.journal:
            by_kind[r.kind.value] = by_kind.get(r.kind.value, 0) + 1
        current: dict[str, int] = {}
        for rec in lc.records:
            current[rec.status.value] = current.get(rec.status.value, 0) + 1
        return {"evaluations": st.evaluations, "long": st.long, "short": st.short, "none": st.none,
                "candidates_by_status": dict(sorted(current.items())),        # current status of each record
                "status_events": dict(sorted(lc.counts.items())),             # registrations + transitions
                "transitions": len(lc.transitions),
                "journal_records": len(self.journal), "journal_by_kind": dict(sorted(by_kind.items())),
                "decision_checkpoints": len(self.checkpoints), "active": len(lc.active), "dropped": self.dropped}
