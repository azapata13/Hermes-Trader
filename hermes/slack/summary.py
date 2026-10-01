"""Trader-facing Slack summary: the few facts needed to decide ENTER / REJECT in 5–10 seconds.

Pure and deterministic: derived ONLY from structured ``ApprovalPayload`` fields (timeframe results,
condition names and pass/fail status, order-flow votes, volumes). Free-text ``detail`` strings are
never parsed. The complete technical view stays in the decision journal, the approval journal and
the logs; nothing here changes a decision or the approval path.

Historical statistics are shown ONLY from an explicit ``HistoryStat`` backed by recorded data with
a defined outcome and comparable-set selection. No source exists yet, so nothing is ever shown.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.decision.approval import ApprovalPayload, TimeframeSummary
from hermes.decision.reasons import Severity

VOLUME_LOW = 0.67            # trigger 30 s volume / average 30 s volume of the 1 m setup window
VOLUME_HIGH = 1.5
BALANCED_BAND = (45, 55)     # buyers' % of KNOWN volume inside this band -> "Balanced"
HISTORY_MIN_SAMPLES = 30          # never show a historical win rate on fewer comparable setups
MAX_REASONS = 3
MAX_WARNINGS = 2

_HOLD_TEXT = {
    "crossed_book_transition": "crossed book",
    "unsorted_book_transition": "unsorted book",
    "empty_side_transition": "empty book side",
}


@dataclass(frozen=True, slots=True)
class HistoryStat:
    """Recorded outcomes of comparable past setups. Displayed only when fully defined."""
    wins: int
    samples: int
    outcome_definition: str          # e.g. "target +1R hit before stop, same session"
    selection_definition: str        # e.g. "LONG continuation, RTH, 5m/1m/30s aligned, trade-flow confirmed"
    source: str                      # recorded data set the numbers come from

    def displayable(self, min_samples: int = HISTORY_MIN_SAMPLES) -> bool:
        return (self.samples >= min_samples and 0 <= self.wins <= self.samples
                and bool(self.outcome_definition.strip()) and bool(self.selection_definition.strip())
                and bool(self.source.strip()))


@dataclass(frozen=True, slots=True)
class TradeSummary:
    headline: str
    risk: str
    timeframes: str
    context: str
    confirmation: str
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    history: str | None
    footer: str


def _num(x: float) -> str:
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def _px(units: int | None, upp: int | None) -> str:
    if units is None:
        return "-"
    return f"{units / upp:.2f}" if upp else f"{units} units"


def _arrow(result: str) -> str:
    return {"LONG": "▲", "SHORT": "▼"}.get(result, "▬")


def _tf(label: str, t: TimeframeSummary, upp: int | None) -> str:
    move = ""
    if t.net_change_units is not None and upp:
        move = f" {t.net_change_units / upp:+.2f}"
    return f"{label} {_arrow(t.result)}{move}"


def _passed(t: TimeframeSummary, name: str) -> bool:
    return any(c[0] == name and c[1] == "pass" for c in t.conditions)


def _vwap(p: ApprovalPayload) -> str:
    names = {c[0] for c in p.regime_5m.conditions}
    if "mid_above_rth_vwap" in names:
        return "VWAP above RTH" if _passed(p.regime_5m, "mid_above_rth_vwap") else "VWAP not above RTH"
    if "mid_below_rth_vwap" in names:
        return "VWAP below RTH" if _passed(p.regime_5m, "mid_below_rth_vwap") else "VWAP not below RTH"
    return "VWAP n/a"


def volume_intensity(p: ApprovalPayload) -> str:
    s, t = p.setup_1m, p.trigger_30s
    setup_total = s.buy_volume + s.sell_volume + s.unknown_volume
    trig_total = t.buy_volume + t.sell_volume + t.unknown_volume
    if not s.bars_used or setup_total <= 0:
        return "Volume n/a"
    avg_30s = setup_total / (2 * s.bars_used)             # 1 m bars -> per 30 s
    ratio = trig_total / avg_30s
    return "Volume " + ("LOW" if ratio < VOLUME_LOW else "HIGH" if ratio > VOLUME_HIGH else "NORMAL")


def dominance(p: ApprovalPayload) -> str:
    s = p.setup_1m
    known = s.buy_volume + s.sell_volume                    # UNKNOWN is never redistributed
    if known <= 0:
        return "Flow n/a"
    buy_pct = round(100 * s.buy_volume / known)
    lo, hi = BALANCED_BAND
    if lo <= buy_pct <= hi:
        return f"Balanced {buy_pct}/{100 - buy_pct}"
    return f"Buyers {buy_pct}%" if buy_pct > hi else f"Sellers {100 - buy_pct}%"


def _votes(p: ApprovalPayload) -> dict[str, str]:
    return {c.name: c.vote.value for c in p.orderflow_evidence}


def _mark(vote: str | None, direction: str) -> str:
    if vote == direction:
        return "✓"
    if vote in ("LONG", "SHORT"):
        return "✗"
    return "–"


def confirmation(p: ApprovalPayload) -> str:
    v, d = _votes(p), p.direction
    return f"Tape {_mark(v.get('trade_flow'), d)} · OFI {_mark(v.get('ofi'), d)} · Sweep {_mark(v.get('sweep_follow'), d)}"


def strongest_reasons(p: ApprovalPayload) -> tuple[str, ...]:
    d = p.direction
    long_ = d == "LONG"
    v = _votes(p)
    out: list[str] = []
    # order = the trader's reading order: tape, timeframe alignment, VWAP, then other flow evidence
    if v.get("trade_flow") == d:
        out.append("Aggressive buying" if long_ else "Aggressive selling")
    if all(t.result == d for t in (p.regime_5m, p.setup_1m, p.trigger_30s)):
        out.append("5m/1m/30s aligned")
    vw = "mid_above_rth_vwap" if long_ else "mid_below_rth_vwap"
    if _passed(p.regime_5m, vw):
        out.append("Above RTH VWAP" if long_ else "Below RTH VWAP")
    if v.get("sweep_follow") == d:
        out.append("Sweep with follow-through")
    if v.get("ofi") == d:
        out.append("Book pressure confirms (OFI)")
    if v.get("absorption_compatible") == d:
        out.append("Sellers absorbed at bid" if long_ else "Buyers absorbed at ask")
    if v.get("microprice") == d:
        out.append("Microprice leans with trade")
    return tuple(out[:MAX_REASONS])


def decisional_warnings(p: ApprovalPayload) -> tuple[str, ...]:
    """Only conditions that should change the decision: flow AGAINST the trade, UNKNOWN-dominated
    trigger volume, or the price on the wrong side of the full-session VWAP. Never permanent."""
    d = p.direction
    if d not in ("LONG", "SHORT"):
        return ()
    long_ = d == "LONG"
    opp = "SHORT" if long_ else "LONG"
    v = _votes(p)
    out: list[str] = []
    if v.get("trade_flow") == opp:
        out.append("⚠ Flow conflict: aggressive " + ("selling" if long_ else "buying") + " on tape")
    if v.get("sweep_follow") == opp:
        out.append("⚠ Flow conflict: " + ("sell" if long_ else "buy") + " sweep with follow-through")
    if v.get("ofi") == opp:
        out.append("⚠ Flow conflict: book pressure against " + d)
    if v.get("absorption_compatible") == opp:
        out.append("⚠ Flow conflict: " + ("sellers absorbing buys at ask" if long_ else "buyers absorbing sells at bid"))
    codes = {r.code for r in p.reasons if r.severity is Severity.CAUTION}
    if "trigger_bar_mostly_unknown_aggressor" in codes:
        out.append("⚠ Trigger volume mostly UNKNOWN aggressor")
    if ("mid_below_full_session_vwap" if long_ else "mid_above_full_session_vwap") in codes:
        out.append("⚠ " + ("Below" if long_ else "Above") + " full-session VWAP")
    return tuple(out[:MAX_WARNINGS])


def history_line(h: HistoryStat | None) -> str | None:
    if h is None or not h.displayable():
        return None
    return f"History: {round(100 * h.wins / h.samples)}% win · {h.samples} similar setups"


def remaining_s(p: ApprovalPayload) -> int | None:
    if p.expires_at_ns is None or p.safety_evaluated_wall_ns is None:
        return None
    return max(0, (p.expires_at_ns - p.safety_evaluated_wall_ns) // 1_000_000_000)


def hold_reason(p: ApprovalPayload) -> str | None:
    holds = [r.code for r in p.reasons if r.severity is Severity.HOLD]
    for code in holds:
        if code in _HOLD_TEXT:
            return _HOLD_TEXT[code]
    return "market-data check" if holds else None


def summarize(p: ApprovalPayload, history: HistoryStat | None = None) -> TradeSummary:
    upp = p.units_per_point
    icon = {"LONG": "🟢", "SHORT": "🔴"}.get(p.direction, "⚪")
    risk = "SL " + _px(p.proposed_stop, upp)
    if p.risk_points is not None:
        risk += f" · Risk {_num(p.risk_points)} pt"
        if p.risk_usd_per_contract is not None:
            risk += f" (${_num(p.risk_usd_per_contract)})"
    rem = remaining_s(p)
    live = p.status == "ACTIONABLE"
    footer = "Intent only — no order sent" + (f" · valid {rem} s" if live and rem is not None else "")
    return TradeSummary(
        headline=f"{icon} {p.direction} {p.symbol} · Entry {_px(p.entry_reference, upp)}",
        risk=risk,
        timeframes=" · ".join((_tf("5m", p.regime_5m, upp), _tf("1m", p.setup_1m, upp),
                               _tf("30s", p.trigger_30s, upp))),
        context=" · ".join((_vwap(p), volume_intensity(p), dominance(p))),
        confirmation=confirmation(p),
        reasons=strongest_reasons(p),
        warnings=decisional_warnings(p),
        history=history_line(history),
        footer=footer,
    )
