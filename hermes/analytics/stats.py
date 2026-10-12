"""V1 Pro D5/D7 + missed-opportunity analytics over recorded setup rows. Pure functions.

Definitions (also printed by reports, so a number never travels without its meaning):

- eligible outcome: ``outcome.status`` STOPPED or COMPLETE (INCOMPLETE / OPEN are excluded);
- +kR: an eligible print reached entry ± k x risk before the stop (excursions freeze at the stop);
- continuation: +1R reached (same as +1R; named for the History line);
- stop-first: the stop was touched before +1R;
- MFE / MAE: max favorable / adverse excursion vs the ORIGINAL entry, in points;
- result at N min: signed excursion at the first eligible print at/after N minutes after creation.

Every statistic is labelled SHADOW unless computed on rows with ``execution == "REAL"`` (V1 never
writes such rows). Shadow and real rows are never mixed in one statistic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from hermes.decision.features import (
    COMPARABLE_FIELDS,
    FEATURES_VERSION,
    comparable_key,
    describe_key,
)

HISTORY_MIN_SAMPLES = 30           # never present a historical statistic on fewer comparable setups
ELIGIBLE = frozenset({"STOPPED", "COMPLETE"})
OUTCOME_DEFINITION = "continuation = +1R reached before the stop within 15 min (eligible prints, shadow fill at the proposed entry)"


def _upp(row: dict) -> int:
    return int(row.get("units_per_point") or 4)


def _pt(row: dict, units) -> float | None:
    return None if units is None else units / _upp(row)


def _horizon(row: dict, seconds: int):
    hz = row.get("horizons_s") or [60, 180, 300, 600, 900]
    vals = row["outcome"].get("horizon_units") or []
    try:
        i = list(hz).index(seconds)
    except ValueError:
        return None
    return vals[i] if i < len(vals) else None


def _r_hit(row: dict, k: int) -> bool:
    r = row["outcome"].get("r_wall_ns") or []
    return len(r) >= k and r[k - 1] is not None


def eligible(rows: list[dict], execution: str = "SHADOW") -> list[dict]:
    """Rows of ONE execution kind with a final, complete-enough outcome."""
    return [r for r in rows if r.get("execution", "SHADOW") == execution and r["outcome"].get("status") in ELIGIBLE]


@dataclass(frozen=True, slots=True)
class OutcomeStats:
    execution: str
    samples: int
    plus_1r: int
    plus_2r: int
    plus_3r: int
    stop_first: int
    median_mfe_pt: float | None
    median_mae_pt: float | None
    median_result_5m_pt: float | None
    median_result_10m_pt: float | None

    def rate(self, n: int) -> float | None:
        return None if not self.samples else n / self.samples


def outcome_stats(rows: list[dict], execution: str = "SHADOW") -> OutcomeStats:
    ok = eligible(rows, execution)

    def med(vals):
        vals = [v for v in vals if v is not None]
        return float(median(vals)) if vals else None

    return OutcomeStats(
        execution, len(ok),
        sum(1 for r in ok if _r_hit(r, 1)), sum(1 for r in ok if _r_hit(r, 2)), sum(1 for r in ok if _r_hit(r, 3)),
        sum(1 for r in ok if r["outcome"].get("stop_touched") and not _r_hit(r, 1)),
        med(_pt(r, r["outcome"].get("mfe_units")) for r in ok),
        med(_pt(r, r["outcome"].get("mae_units")) for r in ok),
        med(_pt(r, _horizon(r, 300)) for r in ok),
        med(_pt(r, _horizon(r, 600)) for r in ok),
    )


@dataclass(frozen=True, slots=True)
class Comparable:
    """Historical comparison for one setup: the definition, the matching rows' stats, displayability."""
    key: tuple[str, ...] | None
    selection_definition: str
    stats: OutcomeStats | None
    source: str
    min_samples: int = HISTORY_MIN_SAMPLES

    @property
    def displayable(self) -> bool:
        return self.stats is not None and self.stats.samples >= self.min_samples


def comparable(features, rows: list[dict], *, exclude_setup_id: str | None = None,
               source: str = "setups.jsonl", min_samples: int = HISTORY_MIN_SAMPLES) -> Comparable:
    """Shadow statistics of setups whose COMPARABLE_FIELDS all equal this setup's (similarity v1)."""
    key = comparable_key(features)
    if key is None:
        return Comparable(None, "unavailable (missing or unversioned characteristics)", None, source, min_samples)
    matches = [r for r in rows if r.get("setup_id") != exclude_setup_id and comparable_key(r.get("features") or {}) == key]
    stats = outcome_stats(matches, "SHADOW")
    definition = f"similarity v{FEATURES_VERSION}: equal {', '.join(COMPARABLE_FIELDS)} ({describe_key(key)})"
    return Comparable(key, definition, stats, f"{source}: {stats.samples} recorded shadow setups", min_samples)


# ---------------------------------------------------------------------------- D5 dispositions

def approvals_by_proposal(approval_rows: list[dict]) -> dict[str, str]:
    """proposal_id -> APPROVED_INTENT / REJECTED from the approval journal (accepted human intents only)."""
    out: dict[str, str] = {}
    for a in approval_rows:
        if not a.get("accepted_human_intent"):
            continue
        pid = a.get("proposal_id")
        if not pid:
            continue
        out[pid] = "APPROVED_INTENT" if a.get("action") == "ENTER" else "REJECTED"
    return out


def disposition(row: dict, approvals: dict[str, str]) -> str:
    """APPROVED_INTENT / REJECTED / INVALIDATED / EXPIRED_UNAPPROVED (= missed) / OPEN_WINDOW.

    APPROVED_INTENT is human intent only: V1 executes nothing, so even an approved setup stays a
    shadow measurement (never P&L)."""
    a = approvals.get(row.get("proposal_id") or "")
    if a is not None:
        return a
    close = row.get("window_close_status")
    if close == "INVALIDATED" or (close is None and row.get("phase") == "INVALIDATED"):
        return "INVALIDATED"
    if close in ("STALE", "EXPIRED", "BLOCKED"):
        return "EXPIRED_UNAPPROVED"
    return "OPEN_WINDOW"


@dataclass(slots=True)
class MissedReport:
    detected: int = 0
    by_disposition: dict[str, int] = field(default_factory=dict)
    incomplete: int = 0
    eligible: int = 0
    continued_after_close_1r: int = 0           # missed / closed windows that later reached +1R vs original entry
    stale_then_20pt: list[str] = field(default_factory=list)
    stale_then_2r: list[str] = field(default_factory=list)
    quick_invalidation: list[str] = field(default_factory=list)   # stopped within 60 s of creation
    low_mae_high_mfe: list[str] = field(default_factory=list)     # MAE <= 0.5R and MFE >= 2R
    rearmed: int = 0
    shadow: OutcomeStats | None = None
    real: OutcomeStats | None = None
    time_to_mfe_s: dict[str, int] = field(default_factory=dict)

    def lines(self) -> list[str]:
        s = self.shadow
        out = [
            "REAL TRADES      none executed (V1 has no execution layer; approvals are human intent only)"
            if not (self.real and self.real.samples) else f"REAL TRADES      {self.real.samples}",
            f"SHADOW SETUPS    detected {self.detected}  eligible outcomes {self.eligible}  incomplete {self.incomplete}",
            f"dispositions     {dict(sorted(self.by_disposition.items()))}",
        ]
        if s and s.samples:
            out += [
                f"+1R {s.plus_1r}/{s.samples}  +2R {s.plus_2r}/{s.samples}  +3R {s.plus_3r}/{s.samples}  "
                f"stop-first {s.stop_first}/{s.samples}  (shadow, {OUTCOME_DEFINITION})",
                f"median MFE {s.median_mfe_pt} pt  median MAE {s.median_mae_pt} pt  median result 5m "
                f"{s.median_result_5m_pt} pt  10m {s.median_result_10m_pt} pt",
            ]
        out += [
            f"after window closed: {self.continued_after_close_1r} reached +1R vs the original entry; "
            f"re-armed {self.rearmed}",
            f"time to MFE        {dict(sorted(self.time_to_mfe_s.items()))}",
            f"STALE/EXPIRED then +20 pt   {len(self.stale_then_20pt)} {self.stale_then_20pt[:10]}",
            f"STALE/EXPIRED then +2R      {len(self.stale_then_2r)} {self.stale_then_2r[:10]}",
            f"stopped within 60 s         {len(self.quick_invalidation)} {self.quick_invalidation[:10]}",
            f"MAE <= 0.5R and MFE >= 2R   {len(self.low_mae_high_mfe)} {self.low_mae_high_mfe[:10]}",
        ]
        return out


def _bucket_s(seconds: float) -> str:
    for limit in (60, 180, 300, 600, 900):
        if seconds <= limit:
            return f"<={limit}s"
    return ">900s"


def missed_report(rows: list[dict], approval_rows: list[dict] | None = None) -> MissedReport:
    approvals = approvals_by_proposal(approval_rows or [])
    rep = MissedReport(detected=len(rows))
    for r in rows:
        d = disposition(r, approvals)
        rep.by_disposition[d] = rep.by_disposition.get(d, 0) + 1
        o = r["outcome"]
        if o.get("status") not in ELIGIBLE:
            rep.incomplete += 1
            continue
        rep.eligible += 1
        risk = r.get("risk_units") or 0
        upp = _upp(r)
        sid = r["setup_id"]
        after = o.get("mfe_after_close_units")
        closed = r.get("window_close_status") in ("STALE", "EXPIRED", "BLOCKED")
        if r.get("rearmed_by"):
            rep.rearmed += 1
        if closed and after is not None and risk and after >= risk:
            rep.continued_after_close_1r += 1
        if closed and after is not None and after >= 20 * upp:
            rep.stale_then_20pt.append(sid)
        if closed and after is not None and risk and after >= 2 * risk:
            rep.stale_then_2r.append(sid)
        if o.get("stop_touched") and o.get("stop_wall_ns") and o["stop_wall_ns"] - r["created_wall_ns"] <= 60 * 10**9:
            rep.quick_invalidation.append(sid)
        if risk and o.get("mae_units", 0) * 2 <= risk and o.get("mfe_units", 0) >= 2 * risk:
            rep.low_mae_high_mfe.append(sid)
        if o.get("mfe_wall_ns") is not None:
            b = _bucket_s((o["mfe_wall_ns"] - r["created_wall_ns"]) / 1e9)
            rep.time_to_mfe_s[b] = rep.time_to_mfe_s.get(b, 0) + 1
    rep.shadow = outcome_stats(rows, "SHADOW")
    rep.real = outcome_stats(rows, "REAL")
    return rep


def excursion_quality(row: dict) -> float | None:
    """Shadow setup QUALITY (not profit): MFE / (MAE + 1 tick), both vs the original entry."""
    o = row.get("outcome") or {}
    if o.get("status") not in ELIGIBLE:
        return None
    return o.get("mfe_units", 0) / (o.get("mae_units", 0) + 1)
