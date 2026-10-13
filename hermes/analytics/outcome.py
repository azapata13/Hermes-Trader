"""V1 Pro — outcome of every ACTIONABLE setup: SHADOW (not taken / no execution) vs REAL (executed).

Two structurally separate worlds:

* **SHADOW** — a measurement made AFTER the signal, from eligible prints only, assuming a hypothetical
  fill at the proposed entry. It comes from the setup tracker (``setups.jsonl`` rows,
  ``"execution": "SHADOW"``). It is never P&L, never a trade, and never fed back into a decision.
* **REAL** — a trade that was actually executed (actual fills). V1 has NO execution layer, so nothing
  writes REAL rows today; the schema and the store exist so that a future, separately-approved
  execution phase records into ``real_trades.jsonl`` and can never be confused with SHADOW.

A human ENTER in V1 is an *intent*: it sends no order, so its outcome is still SHADOW
("ENTER intent — no order sent").

Shadow outcome rule (documented, deterministic, decision-time information never altered):

* ``STOPPED``  → outcome = −risk (the original stop was touched before the end of the horizon;
  excursions are frozen at that touch, so "+X reached" means reached BEFORE the stop);
* ``COMPLETE`` → outcome = signed result at the last horizon (15 min, first eligible print at/after it);
* ``OPEN`` / ``INCOMPLETE`` → no outcome (excluded from statistics; nothing is interpolated).
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

POINT_TARGETS = (10, 20, 30)
SHADOW_LABEL = "SHADOW ONLY — NOT REAL P&L"
REAL_LABEL = "REAL TRADE"
OUTCOME_RULE = ("stop touched first → −risk; otherwise signed result at the 15 min horizon; "
                "open/incomplete → no outcome")
DEFAULT_REAL_STORE = "~/hermes-data/trades/real_trades.jsonl"


def _upp(row: dict) -> int:
    return int(row.get("units_per_point") or 4)


# ---------------------------------------------------------------------------------------------- SHADOW

@dataclass(frozen=True, slots=True)
class ShadowResult:
    setup_id: str
    direction: str
    symbol: str
    entry_pt: float
    stop_pt: float
    risk_pt: float
    status: str                         # OPEN / STOPPED / COMPLETE / INCOMPLETE
    mfe_pt: float
    mae_pt: float
    hits: tuple[tuple[int, bool], ...]  # ((10, True), (20, True), (30, False))
    stop_touched: bool
    outcome_pt: float | None            # per OUTCOME_RULE; None when not final/eligible
    duration_s: float | None
    incomplete_reason: str | None = None
    execution: str = "SHADOW"

    @property
    def final(self) -> bool:
        return self.outcome_pt is not None

    def hit(self, target: int) -> bool:
        return dict(self.hits).get(target, False)


def shadow_result(row: dict) -> ShadowResult:
    """From a tracker row (``SetupView.row()`` / ``setups.jsonl``). Pure; raises on a REAL row."""
    if row.get("execution", "SHADOW") != "SHADOW":
        raise ValueError("shadow_result() only reads SHADOW rows")
    upp = _upp(row)
    o = row.get("outcome") or {}
    status = o.get("status", "OPEN")
    mfe = (o.get("mfe_units") or 0) / upp
    mae = (o.get("mae_units") or 0) / upp
    risk = (row.get("risk_units") or 0) / upp
    created = row.get("created_wall_ns")
    outcome = duration = None
    if status == "STOPPED":
        outcome = -risk
        if o.get("stop_wall_ns") is not None and created is not None:
            duration = (o["stop_wall_ns"] - created) / 1e9
    elif status == "COMPLETE":
        hz = [h for h in (o.get("horizon_units") or []) if h is not None]
        if hz:
            outcome = hz[-1] / upp
            duration = float(max(row.get("horizons_s") or [900]))
    return ShadowResult(
        setup_id=row.get("setup_id", ""), direction=row.get("direction", ""), symbol=row.get("symbol", ""),
        entry_pt=(row.get("entry_units") or 0) / upp, stop_pt=(row.get("stop_units") or 0) / upp, risk_pt=risk,
        status=status, mfe_pt=mfe, mae_pt=mae, hits=tuple((t, mfe >= t) for t in POINT_TARGETS),
        stop_touched=bool(o.get("stop_touched")), outcome_pt=outcome, duration_s=duration,
        incomplete_reason=o.get("incomplete_reason"))


def _fmt(x: float | None, signed: bool = True) -> str:
    if x is None:
        return "—"
    s = f"{x:+.2f}" if signed else f"{x:.2f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def shadow_result_lines(r: ShadowResult, disposition_text: str) -> list[str]:
    """Slack/report lines. Always labelled SHADOW; never says profit, P&L or trade result."""
    lines = [disposition_text, f"{r.direction} {r.symbol} {_fmt(r.entry_pt, False)} · SL {_fmt(r.stop_pt, False)}",
             "SHADOW RESULT",
             f"MFE {_fmt(r.mfe_pt)} pts · MAE {_fmt(-r.mae_pt) if r.mae_pt else '0'} pts",
             " · ".join(f"+{t}: {'YES' if hit else 'NO'}" for t, hit in r.hits),
             f"Stop touched: {'YES' if r.stop_touched else 'NO'}"
             + (f" · duration {r.duration_s / 60:.1f} min" if r.duration_s is not None else "")]
    if r.final:
        lines.append(f"Outcome: {_fmt(r.outcome_pt)} pts ({r.status.lower()})")
    else:
        lines.append(f"Outcome: not final ({r.status.lower()}"
                     + (f", {r.incomplete_reason}" if r.incomplete_reason else "") + ") — excluded from statistics")
    lines.append(SHADOW_LABEL)
    return lines


DISPOSITION_TEXT = {
    "APPROVED_INTENT": "🟡 ENTER INTENT RECORDED — no order sent (V1 read-only)",
    "REJECTED": "⚪ SETUP NOT TAKEN — rejected",
    "EXPIRED_UNAPPROVED": "⚪ SETUP NOT TAKEN — window expired without decision",
    "INVALIDATED": "⚪ SETUP NOT TAKEN — invalidated before a decision",
    "OPEN_WINDOW": "⚪ SETUP NOT TAKEN",
}


def disposition_text(human_action: str | None, window_close_status: str | None) -> str:
    """Bridge view of what the human did (from the approval workflow) → disposition headline."""
    h = (human_action or "").upper()
    if h.startswith("ENTER"):
        return DISPOSITION_TEXT["APPROVED_INTENT"]
    if h.startswith("REJECT"):
        return DISPOSITION_TEXT["REJECTED"]
    if window_close_status == "INVALIDATED":
        return DISPOSITION_TEXT["INVALIDATED"]
    if window_close_status in ("EXPIRED", "STALE", "BLOCKED"):
        return DISPOSITION_TEXT["EXPIRED_UNAPPROVED"]
    return DISPOSITION_TEXT["OPEN_WINDOW"]


# ---------------------------------------------------------------------------------------------- REAL

REAL_REQUIRED = ("trade_id", "setup_id", "direction", "symbol", "entry_reference", "actual_entry", "stop",
                 "actual_exit", "quantity", "point_value_usd", "entry_wall_ns", "exit_wall_ns", "exit_reason")


@dataclass(frozen=True, slots=True)
class RealTrade:
    """An EXECUTED trade (actual fills). Never produced in V1 (no execution layer)."""
    trade_id: str
    setup_id: str
    direction: str                      # LONG / SHORT
    symbol: str
    entry_reference: float              # proposed entry at decision time
    actual_entry: float
    stop: float
    actual_exit: float
    quantity: int
    point_value_usd: float
    entry_wall_ns: int
    exit_wall_ns: int
    exit_reason: str                    # stop / target / trailing / manual / session_end ...
    mfe_pt: float | None = None
    mae_pt: float | None = None
    trailing: bool = False
    context: dict = field(default_factory=dict)
    execution: str = "REAL"

    def __post_init__(self) -> None:
        if self.execution != "REAL":
            raise ValueError("RealTrade.execution must be REAL")
        if self.direction not in ("LONG", "SHORT"):
            raise ValueError("direction must be LONG or SHORT")
        if self.quantity <= 0 or self.exit_wall_ns < self.entry_wall_ns:
            raise ValueError("invalid quantity or timestamps")

    @property
    def points(self) -> float:
        d = self.actual_exit - self.actual_entry
        return d if self.direction == "LONG" else -d

    @property
    def pnl_usd(self) -> float:
        return self.points * self.point_value_usd * self.quantity

    @property
    def duration_s(self) -> float:
        return (self.exit_wall_ns - self.entry_wall_ns) / 1e9

    def row(self) -> dict:
        d = asdict(self)
        d.update(points=self.points, pnl_usd=self.pnl_usd, duration_s=self.duration_s, label=REAL_LABEL)
        return d


def real_trade_from_row(row: dict) -> RealTrade:
    if row.get("execution") != "REAL":
        raise ValueError("not a REAL trade row")
    missing = [k for k in REAL_REQUIRED if row.get(k) is None]
    if missing:
        raise ValueError(f"REAL trade row missing {missing}")
    keep = {k: row[k] for k in RealTrade.__dataclass_fields__ if k in row}
    return RealTrade(**keep)


class RealTradeStore:
    """Append-only ``real_trades.jsonl``: a different file from the shadow setup store, on purpose."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(os.path.expanduser(str(path or os.getenv("HERMES_REAL_TRADE_STORE") or DEFAULT_REAL_STORE)))
        self._lock = threading.Lock()

    def add(self, trade: RealTrade) -> None:
        if not isinstance(trade, RealTrade):
            raise TypeError("only RealTrade objects are recorded as REAL")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(trade.row(), ensure_ascii=False, sort_keys=True) + "\n")

    def load(self) -> tuple[list[RealTrade], int]:
        trades, bad = [], 0
        if not self.path.exists():
            return trades, bad
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                trades.append(real_trade_from_row(json.loads(line)))
            except (ValueError, TypeError, KeyError):
                bad += 1
        return trades, bad


# ---------------------------------------------------------------------------------------------- report

def _mean(vals: list[float]) -> float | None:
    return round(sum(vals) / len(vals), 2) if vals else None


@dataclass(slots=True)
class PerformanceReport:
    """DECISION-TIME counts (what Hermès proposed, what the human did) are separate from
    POST-SETUP SHADOW outcomes and from REAL executed trades. Nothing here feeds a decision."""
    actionable: int = 0
    by_disposition: dict[str, int] = field(default_factory=dict)
    taken_intent: int = 0
    not_taken: int = 0
    shadow_final: int = 0
    shadow_excluded: int = 0
    shadow_wins: int = 0
    shadow_losses: int = 0
    shadow_flat: int = 0
    shadow_avg_points: float | None = None
    shadow_avg_mfe: float | None = None
    shadow_avg_mae: float | None = None
    shadow_avg_duration_s: float | None = None
    target_hits: dict[int, int] = field(default_factory=dict)
    missed_profitable: list[str] = field(default_factory=list)      # not taken and shadow outcome > 0
    missed_20pt: list[str] = field(default_factory=list)            # not taken and +20 reached before stop
    real_trades: int = 0
    real_wins: int = 0
    real_losses: int = 0
    real_avg_points: float | None = None
    real_rejected_rows: int = 0

    def rate(self, n: int) -> str:
        return f"{100 * n / self.shadow_final:.0f}%" if self.shadow_final else "—"

    def lines(self) -> list[str]:
        out = ["DECISION-TIME", f"  actionable setups {self.actionable}",
               "  by disposition   " + (", ".join(f"{k} {v}" for k, v in sorted(self.by_disposition.items())) or "—"),
               f"  ENTER intents {self.taken_intent} (no order sent in V1) · not taken {self.not_taken}",
               "POST-SETUP SHADOW OUTCOME (" + SHADOW_LABEL + ")",
               f"  rule: {OUTCOME_RULE}",
               f"  final {self.shadow_final} · excluded (open/incomplete) {self.shadow_excluded}",
               f"  wins {self.shadow_wins} · losses {self.shadow_losses} · flat {self.shadow_flat}",
               f"  avg outcome {_fmt(self.shadow_avg_points)} pts · avg MFE {_fmt(self.shadow_avg_mfe)} · "
               f"avg MAE {_fmt(self.shadow_avg_mae, False)} · avg duration "
               + (f"{self.shadow_avg_duration_s / 60:.1f} min" if self.shadow_avg_duration_s is not None else "—"),
               "  " + " · ".join(f"+{t} {self.target_hits.get(t, 0)} ({self.rate(self.target_hits.get(t, 0))})"
                                 for t in POINT_TARGETS),
               f"  missed profitable {len(self.missed_profitable)} · missed +20 pt {len(self.missed_20pt)}",
               "REAL TRADES (" + REAL_LABEL + ")"]
        if self.real_trades:
            out.append(f"  trades {self.real_trades} · wins {self.real_wins} · losses {self.real_losses} · "
                       f"avg {_fmt(self.real_avg_points)} pts")
        else:
            out.append("  none executed")
        if self.real_rejected_rows:
            out.append(f"  {self.real_rejected_rows} malformed REAL rows ignored")
        return out


def performance_report(setup_rows: list[dict], approval_rows: list[dict] | None = None,
                       real_trades: list[RealTrade] | None = None, real_rejected_rows: int = 0) -> PerformanceReport:
    from hermes.analytics.stats import approvals_by_proposal, disposition  # same disposition rules as D5

    approvals = approvals_by_proposal(approval_rows or [])
    rep = PerformanceReport(real_rejected_rows=real_rejected_rows)
    pts, mfes, maes, durs = [], [], [], []
    for row in setup_rows:
        if row.get("execution", "SHADOW") != "SHADOW":
            continue                                   # never mix: REAL comes only from RealTrade
        rep.actionable += 1
        d = disposition(row, approvals)
        rep.by_disposition[d] = rep.by_disposition.get(d, 0) + 1
        taken = d == "APPROVED_INTENT"
        rep.taken_intent += taken
        rep.not_taken += not taken
        r = shadow_result(row)
        if not r.final:
            rep.shadow_excluded += 1
            continue
        rep.shadow_final += 1
        pts.append(r.outcome_pt)
        mfes.append(r.mfe_pt)
        maes.append(r.mae_pt)
        if r.duration_s is not None:
            durs.append(r.duration_s)
        if r.outcome_pt > 0:
            rep.shadow_wins += 1
        elif r.outcome_pt < 0:
            rep.shadow_losses += 1
        else:
            rep.shadow_flat += 1
        for t, hit in r.hits:
            rep.target_hits[t] = rep.target_hits.get(t, 0) + hit
        if not taken and r.outcome_pt > 0:
            rep.missed_profitable.append(r.setup_id)
        if not taken and r.hit(20):
            rep.missed_20pt.append(r.setup_id)
    rep.shadow_avg_points, rep.shadow_avg_mfe = _mean(pts), _mean(mfes)
    rep.shadow_avg_mae, rep.shadow_avg_duration_s = _mean(maes), _mean(durs)
    real = list(real_trades or [])
    rep.real_trades = len(real)
    rep.real_wins = sum(1 for t in real if t.points > 0)
    rep.real_losses = sum(1 for t in real if t.points < 0)
    rep.real_avg_points = _mean([t.points for t in real])
    return rep
