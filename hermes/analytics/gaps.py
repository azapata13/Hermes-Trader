"""V1 Pro — "what happened between two real signals?" (gap / missed-move analysis). Read-only.

Answers, for every window between two consecutive REAL Hermès signals (``APPROVAL_VIEW_CREATED``
decision-journal records = the proposals Slack would show), from RECORDED data only:

- were there real candidates (ACTIONABLE), blocked candidates, or no directional candidate at all,
  and the blocking reasons when nothing was sent;
- what the 5m / 1m / 30s stages, VWAP relation, tape / OFI / sweep votes said at each 30 s decision;
- whether an earlier setup was still MONITORING (window closed) or was re-armed;
- how far the market moved (max excursion up / down, time to extreme) and with what data quality
  (market data OK at evaluations, time not OK, tape continuity breaks, alerts).

A significant move without a real candidate is reported as ``MOVE_WITHOUT_CANDIDATE`` with a
**reconstructed shadow excursion**: hindsight measurement from the first print of the window, for
analysis only. It is never a signal Hermès sent, never executed, never P&L, and it is never written
to the decision journal, the setup store or Slack.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise

_S = 1_000_000_000
RECONSTRUCTED_LABEL = "RECONSTRUCTED SHADOW (hindsight analysis — not a Hermès signal, not executed, not P&L)"


# ---------------------------------------------------------------------------- recorded inputs

@dataclass(frozen=True, slots=True)
class EvalPoint:
    """One 30 s decision point, from the C9 candidate the engine actually evaluated."""
    wall_ns: int
    setup_id: str
    status: str                      # NONE / BLOCKED / ACTIONABLE (initial C9 status)
    direction: str                   # LONG / SHORT / NONE
    regime_5m: str
    setup_1m: str
    trigger_30s: str
    vwap: str                        # above / below / unavailable (mid vs RTH VWAP at the 5m stage)
    tape: str                        # vote LONG / SHORT / NEUTRAL / UNAVAILABLE
    ofi: str
    sweep: str
    market_data_ok: bool
    session_rth: bool
    block_codes: tuple[str, ...]     # structured BLOCK reasons (safety + stages), "source:code"


def _vote(c, name: str) -> str:
    for comp in c.orderflow_evidence:
        if comp.name == name:
            return comp.vote.value
    return "UNAVAILABLE"


def _vwap(c) -> str:
    for cond in c.regime_5m.conditions:
        if cond.name == "mid_above_rth_vwap" and cond.status == "pass":
            return "above"
        if cond.name == "mid_below_rth_vwap" and cond.status == "pass":
            return "below"
    names = {cond.name: cond.status for cond in c.regime_5m.conditions}
    if names.get("mid_above_rth_vwap") == "fail" or names.get("mid_below_rth_vwap") == "fail":
        return "below" if names.get("mid_above_rth_vwap") == "fail" else "above"
    return "unavailable"


def eval_points(records) -> list[EvalPoint]:
    """From LifecycleTracker records (every evaluated candidate, any status)."""
    out = []
    for rec in records:
        c = rec.candidate
        blocks = tuple(f"{r.source}:{r.code}" for r in c.reasons if r.severity.value == "BLOCK")
        first_status = rec.transitions[0].from_status.value if rec.transitions else rec.status.value
        out.append(EvalPoint(c.wall_ns, rec.setup_id, first_status, c.direction.value, c.regime_5m.result,
                             c.setup_1m.result, c.trigger_30s.result, _vwap(c), _vote(c, "trade_flow"),
                             _vote(c, "ofi"), _vote(c, "sweep_follow"), c.market_data_ok, c.session_rth, blocks))
    return sorted(out, key=lambda e: (e.wall_ns, e.setup_id))


class PathObserver:
    """Replay observer: eligible prints, market-data health at timer ticks, tape breaks, alerts.

    Read-only on the engine (same contract as every replay observer)."""

    def __init__(self, engine, instrument_id: int | None = None) -> None:
        self.engine = engine
        self.iid = instrument_id
        self.prints: list[tuple[int, int]] = []              # (wall_ns, price_units)
        self.health: list[tuple[int, bool, tuple[str, ...]]] = []   # transitions only
        self.tape_breaks: list[int] = []
        self.alerts: list[tuple[int, str]] = []
        self._last_seq = -1
        self._epoch: int | None = None
        self._ok: bool | None = None
        self._seen_alerts: set[str] = set()
        self.first_wall_ns: int | None = None
        self.last_wall_ns: int | None = None

    def _inst(self):
        insts = self.engine.instruments
        if self.iid is not None:
            return insts.get(self.iid)
        return next(iter(insts.values())) if insts else None

    def after_event(self, raw, events, now_mono_ns: int) -> None:
        if self.first_wall_ns is None:
            self.first_wall_ns = raw.recv_wall_ns
        self.last_wall_ns = raw.recv_wall_ns
        inst = self._inst()
        if inst is None or inst.tape is None:
            return
        last = inst.tape.last()
        if last is not None and last.seq > self._last_seq:
            self._last_seq = last.seq
            if last.eligible:
                self.prints.append((last.recv_wall_ns, last.price_units))
        if not self.prints:
            self._epoch = inst.tape.epoch            # initial subscription is not a break
        elif inst.tape.epoch != self._epoch:
            self._epoch = inst.tape.epoch
            self.tape_breaks.append(raw.recv_wall_ns)
        for key in self.engine.alerts:
            if key not in self._seen_alerts:
                self._seen_alerts.add(key)
                self.alerts.append((raw.recv_wall_ns, key))
        if type(raw).__name__ == "RawTimerTick":
            reasons = tuple(self.engine.market_data_reasons(inst))
            ok = not reasons
            if ok != self._ok:
                self._ok = ok
                self.health.append((raw.recv_wall_ns, ok, reasons[:4]))


# ---------------------------------------------------------------------------- analysis

@dataclass(frozen=True, slots=True)
class Excursion:
    direction: str
    start_wall_ns: int
    start_price_units: int
    mfe_units: int
    mfe_wall_ns: int | None
    mae_units: int
    mae_wall_ns: int | None
    label: str = RECONSTRUCTED_LABEL


def excursion(prints: list[tuple[int, int]], direction: str) -> Excursion | None:
    if not prints:
        return None
    d = 1 if direction == "LONG" else -1
    t0, p0 = prints[0]
    mfe = mae = 0
    mfe_t = mae_t = None
    for t, p in prints[1:]:
        fav = d * (p - p0)
        if fav > mfe:
            mfe, mfe_t = fav, t
        if -fav > mae:
            mae, mae_t = -fav, t
    return Excursion(direction, t0, p0, mfe, mfe_t, mae, mae_t)


@dataclass(slots=True)
class WindowReport:
    start_wall_ns: int
    end_wall_ns: int
    opened_by: str | None = None             # setup_id of the real signal that opened the window
    opened_direction: str | None = None
    opener_setup: str | None = None          # tracked state of the opening setup at the window end
    closed_by: str | None = None
    evaluations: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    directional: dict[str, int] = field(default_factory=dict)      # NONE/LONG/SHORT candidate directions
    actionable: list[str] = field(default_factory=list)
    top_block_codes: list[tuple[str, int]] = field(default_factory=list)
    stages: dict[str, dict[str, int]] = field(default_factory=dict)   # stage -> result -> count
    md_ok_evaluations: int = 0
    not_ok_seconds: float = 0.0
    tape_breaks: int = 0
    alerts: list[str] = field(default_factory=list)
    monitoring_setups: list[str] = field(default_factory=list)
    rearms: list[str] = field(default_factory=list)
    prints: int = 0
    open_units: int | None = None
    high_units: int | None = None
    low_units: int | None = None
    shadow: dict[str, Excursion] = field(default_factory=dict)
    classification: list[str] = field(default_factory=list)
    why_no_signal: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return (self.end_wall_ns - self.start_wall_ns) / _S


def _overlap_not_ok(health, start: int, end: int, session_end: int) -> float:
    total = 0
    for i, (t, ok, _) in enumerate(health):
        nxt = health[i + 1][0] if i + 1 < len(health) else session_end
        if ok:
            continue
        a, b = max(t, start), min(nxt, end)
        if b > a:
            total += b - a
    return total / _S


def analyze_windows(evals: list[EvalPoint], prints: list[tuple[int, int]], signals: list[tuple[int, str]],
                    *, session_start: int, session_end: int, units_per_point: int = 4, min_move_pt: float = 20.0,
                    health=(), tape_breaks=(), alerts=(), setup_views=(), setup_events=()) -> list[WindowReport]:
    """Windows: [session start, signal 1), [signal 1, signal 2), ..., [last signal, session end]."""
    bounds = [(session_start, None)] + sorted(signals) + [(session_end, None)]
    out: list[WindowReport] = []
    min_units = round(min_move_pt * units_per_point)
    direction_of = {e.setup_id: e.direction for e in evals}
    views = {v.setup_id: v for v in setup_views}
    for (a, opener), (b, closer) in pairwise(bounds):
        if b <= a and not (a == b == session_end):
            continue
        w = WindowReport(a, b, opened_by=opener, closed_by=closer, opened_direction=direction_of.get(opener))
        ov = views.get(opener) if opener else None
        if ov is not None:
            o = ov.outcome
            w.opener_setup = (f"{ov.phase.value} window={ov.window_close_status or 'open'} outcome={o.status.value} "
                              f"MFE {o.mfe_units / units_per_point:.2f} pt MAE {o.mae_units / units_per_point:.2f} pt "
                              "(shadow)")
        ev = [e for e in evals if a <= e.wall_ns < b and e.setup_id not in (opener, closer)]
        w.evaluations = len(ev)
        w.by_status = dict(Counter(e.status for e in ev))
        w.directional = dict(Counter(e.direction for e in ev))
        w.actionable = [e.setup_id for e in ev if e.status == "ACTIONABLE"]
        w.top_block_codes = Counter(c for e in ev for c in e.block_codes).most_common(6)
        w.stages = {name: dict(Counter(getattr(e, name) for e in ev))
                    for name in ("regime_5m", "setup_1m", "trigger_30s", "vwap", "tape", "ofi", "sweep")}
        w.md_ok_evaluations = sum(1 for e in ev if e.market_data_ok)
        w.not_ok_seconds = _overlap_not_ok(list(health), a, b, session_end)
        w.tape_breaks = sum(1 for t in tape_breaks if a <= t < b)
        w.alerts = [k for t, k in alerts if a <= t < b]
        w.monitoring_setups = sorted({e.setup_id for e in setup_events
                                      if e.kind == "WINDOW_CLOSED" and e.wall_ns < b}
                                     - {e.setup_id for e in setup_events
                                        if e.kind == "OUTCOME_FINAL" and e.wall_ns < a})
        w.rearms = [e.setup_id for e in setup_events if e.kind == "REARMED" and a <= e.wall_ns < b]
        px = [(t, p) for t, p in prints if a <= t < b]
        w.prints = len(px)
        if px:
            w.open_units = px[0][1]
            w.high_units = max(p for _, p in px)
            w.low_units = min(p for _, p in px)
            for d in ("LONG", "SHORT"):
                x = excursion(px, d)
                if x is not None and x.mfe_units >= min_units:
                    w.shadow[d] = x
        actionable_dirs = {e.direction for e in ev if e.status == "ACTIONABLE"}
        for d in w.shadow:
            if d == w.opened_direction:
                w.classification.append(f"MOVE_WITH_OPENING_SIGNAL:{d}")
            elif d not in actionable_dirs:
                w.classification.append(f"MOVE_WITHOUT_CANDIDATE:{d}")
        if any(e.status == "BLOCKED" for e in ev):
            w.classification.append("CANDIDATE_BLOCKED")
        if w.actionable:
            w.classification.append("REAL_CANDIDATE")
        if w.monitoring_setups:
            w.classification.append("EARLIER_SETUP_MONITORING")
        if w.rearms:
            w.classification.append("REARM")
        if w.not_ok_seconds >= 1.0 or w.tape_breaks or w.alerts or (w.evaluations and w.md_ok_evaluations < w.evaluations):
            w.classification.append("DATA_QUALITY_ISSUES")
        if not w.actionable:
            w.why_no_signal = [f"{c} ×{n}" for c, n in w.top_block_codes] or (
                ["no 30 s decision point in this window"] if not ev else ["no directional candidate (stages neutral)"])
        out.append(w)
    return out


def window_lines(w: WindowReport, units_per_point: int = 4, fmt_time=None) -> list[str]:
    ft = fmt_time or str

    def px(u):
        return "-" if u is None else f"{u / units_per_point:.2f}"

    lines = [f"WINDOW {ft(w.start_wall_ns)} -> {ft(w.end_wall_ns)} ({w.duration_s / 60:.1f} min)  "
             f"opened by {w.opened_by or 'session start'}  closed by {w.closed_by or 'session end'}",
             f"  classification   {', '.join(w.classification) or 'quiet'}",
             f"  decisions        {w.evaluations} evaluations {w.by_status}  directions {w.directional}  "
             f"real candidates {w.actionable or 'none'}",
             "  stages           " + "  ".join(f"{k}={v}" for k, v in w.stages.items()),
             f"  market           prints {w.prints}  open {px(w.open_units)}  high {px(w.high_units)}  "
             f"low {px(w.low_units)}",
             f"  data quality     market data OK at {w.md_ok_evaluations}/{w.evaluations} evaluations, not OK "
             f"{w.not_ok_seconds:.0f}s, tape breaks {w.tape_breaks}, alerts {w.alerts or 'none'}",
             f"  opening signal   {w.opened_by or '-'} {w.opened_direction or ''} {w.opener_setup or ''}".rstrip(),
             f"  setups           followed after window close {w.monitoring_setups or 'none'}  "
             f"re-armed {w.rearms or 'none'}"]
    if w.why_no_signal:
        lines.append(f"  why no signal    {'; '.join(w.why_no_signal)}")
    for d, x in w.shadow.items():
        mfe_s = None if x.mfe_wall_ns is None else (x.mfe_wall_ns - x.start_wall_ns) / _S
        lines.append(f"  {d:5s} excursion  MFE {x.mfe_units / units_per_point:.2f} pt"
                     + (f" after {mfe_s / 60:.1f} min" if mfe_s is not None else "")
                     + f"  MAE {x.mae_units / units_per_point:.2f} pt from {px(x.start_price_units)}  [{x.label}]")
    return lines
