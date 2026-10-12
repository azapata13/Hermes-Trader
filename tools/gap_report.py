#!/usr/bin/env python3
"""V1 Pro gap report: what happened between two REAL Hermès signals? (read-only replay)

    python tools/gap_report.py <session_dir>                 # every window between real signals
    python tools/gap_report.py <session_dir> --min-move 20   # flag moves >= 20 pt without a candidate
    python tools/gap_report.py <session_dir> --from 14:02 --to 14:15   # one UTC time window

Replays the recording with the same decision runtime as live and reports, per window: real / blocked
candidates and blocking reasons, 5m / 1m / 30s / VWAP / tape / OFI / sweep at each 30 s decision,
earlier setups still monitoring or re-armed, the market excursion and data quality. Moves without a
real candidate are shown as RECONSTRUCTED SHADOW excursions: hindsight analysis, never a signal
Hermès sent, never executed, never P&L. Nothing is written anywhere.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes.analytics.gaps import PathObserver, analyze_windows, eval_points, window_lines  # noqa: E402
from hermes.decision.runtime import JournalKind  # noqa: E402
from hermes.replay.runner import ReplayOptions, replay_session  # noqa: E402


def _t(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _clock(day_ns: int, hhmm: str) -> int:
    d = datetime.fromtimestamp(day_ns / 1e9, UTC)
    h, m, *s = (int(x) for x in hhmm.split(":"))
    return int(d.replace(hour=h, minute=m, second=s[0] if s else 0, microsecond=0).timestamp() * 1e9)


def run(session: str, min_move: float = 20.0, t_from: str | None = None, t_to: str | None = None) -> list[str]:
    res = replay_session(session, ReplayOptions(observers=(PathObserver,), compare_live=False))
    obs = res.observers[0]
    drt = res.decisions
    if drt is None or obs.first_wall_ns is None:
        return ["no decision layer or no events in this recording"]
    upp = next((r.candidate.units_per_point for r in drt.driver.lifecycle.records
                if r.candidate.units_per_point), 4)
    signals = [(r.wall_ns, r.setup_id) for r in drt.journal if r.kind is JournalKind.APPROVAL_VIEW_CREATED]
    start, end = obs.first_wall_ns, obs.last_wall_ns
    if t_from or t_to:
        a = _clock(start, t_from) if t_from else start
        b = _clock(start, t_to) if t_to else end
        signals = [s for s in signals if a < s[0] < b]
        start, end = a, b
    windows = analyze_windows(eval_points(drt.driver.lifecycle.records), obs.prints, signals,
                              session_start=start, session_end=end, units_per_point=upp, min_move_pt=min_move,
                              health=obs.health, tape_breaks=obs.tape_breaks, alerts=obs.alerts,
                              setup_events=drt.setups.events, setup_views=list(drt.setups.by_id.values()))
    lines = ["=========== HERMÈS GAP REPORT (recorded data; shadow analysis only — no order was sent) ===========",
             f"session          {res.session_id}  {_t(obs.first_wall_ns)} -> {_t(obs.last_wall_ns)}",
             f"real signals     {len(signals)} (APPROVAL_VIEW_CREATED = what Slack would show)",
             f"move threshold   {min_move:g} pt"]
    for w in windows:
        lines += window_lines(w, upp, _t)
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session")
    ap.add_argument("--min-move", type=float, default=20.0)
    ap.add_argument("--from", dest="t_from")
    ap.add_argument("--to", dest="t_to")
    args = ap.parse_args(argv)
    for line in run(args.session, args.min_move, args.t_from, args.t_to):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
