#!/usr/bin/env python3
"""Replay a recording through Normalizer + MarketEngine and report C5 bars + session context.

    python tools/bar_report.py                         # latest session under ~/hermes-data/recordings
    python tools/bar_report.py <session_dir> --tf 60 --last 20 --grace-ms 250 500 1000
    python tools/bar_report.py --synthetic 20000       # no recording: synthetic MNQ-like session (perf)

Prints, per close-grace value: bar counts per timeframe, flag histogram, BUY/SELL/UNKNOWN volume,
known_delta, late prints (the calibration signal for ``[bars].close_grace_ms``), excluded prints per
reason, session / RTH / overnight VWAP, H/L, previous observed session, the last N bars of ``--tf``,
and per-stage costs (trade -> 30 s update, 30 s -> 1 m, 1 m -> 5 m, snapshot) with bars on vs off.
Deterministic: the same recording + config always yields the same bars. Read-only.
"""

from __future__ import annotations

import argparse
import dataclasses
import random
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes.config import DEFAULT_CONFIG_PATH, load_config  # noqa: E402
from hermes.ibkr.normalizer import Normalizer  # noqa: E402
from hermes.market import bars as bars_mod  # noqa: E402
from hermes.market.bars import BarFlag  # noqa: E402
from hermes.market.engine import MarketEngine  # noqa: E402
from hermes.market.events import ClockTickEvent, TradeEvent  # noqa: E402

_ns = time.perf_counter_ns


def synthetic_events(n_trades: int, seed: int = 11) -> list:
    """Deterministic MNQ-like raw session (US/Central calendar, RTH, ticks, one short outage)."""
    from tests.support import TRADES, RawScript
    t0 = 1790085600                                              # 2026-09-22 14:00 UTC (09:00 CDT, RTH)
    sc = RawScript(wall0=t0 * 10**9)
    sc.bootstrap(trading_hours="20260921:1700-20260922:1600;20260922:1700-20260923:1600",
                 liquid_hours="20260922:0830-20260922:1500", time_zone_id="US/Central")
    sc.seed_book()
    rng = random.Random(seed)
    t = float(t0)
    for i in range(n_trades):
        t += rng.expovariate(4.0)                                # ~4 prints/s
        sc.at(t)
        if rng.random() < 0.25:
            sc.tick()
        sc.trade(TRADES, 21000 + 0.25 * rng.randint(-6, 6), rng.randint(1, 5))
        if i == n_trades // 2:
            sc.error(-1, 1100)
            sc.at(t + 3)
            sc.error(-1, 1102)
            t += 3
    sc.at(t + 301)
    sc.tick()
    return sc.events


def replay(raws, cfg, grace_ms: int, bars_on: bool = True, snapshot_every: int = 0):
    bcfg = dataclasses.replace(cfg.bars, enabled=bars_on, close_grace_ms=grace_ms,
                               history_30s=10**7, history_1m=10**7, history_5m=10**7)
    norm = Normalizer()
    eng = MarketEngine(cfg.book, cfg.session, cfg.subscriptions, tape_cfg=cfg.tape, bars_cfg=bcfg)
    cost = Counter()
    count = Counter()
    n = 0
    for raw in raws:
        for ev in norm.normalize(raw):
            t = type(ev)
            a = _ns()
            eng.on_event(ev)
            d = _ns() - a
            if t is TradeEvent:
                cost["trade"] += d
                count["trade"] += 1
            elif t is ClockTickEvent:
                cost["tick"] += d
                count["tick"] += 1
            n += 1
            if snapshot_every and n % snapshot_every == 0:
                a = _ns()
                eng.snapshot()
                cost["snapshot"] += _ns() - a
                count["snapshot"] += 1
    return eng, cost, count


class _AggTimer:
    """Times the 30 s -> 1 m and 1 m -> 5 m aggregation steps (tool-only monkeypatch)."""

    def __init__(self):
        self.cost, self.count = Counter(), Counter()
        self._orig = bars_mod._Agg.add

    def __enter__(self):
        orig, cost, count = self._orig, self.cost, self.count

        def timed(agg, bar):
            a = _ns()
            out = orig(agg, bar)
            cost[agg.tf] += _ns() - a
            count[agg.tf] += 1
            return out
        bars_mod._Agg.add = timed
        return self

    def __exit__(self, *exc):
        bars_mod._Agg.add = self._orig


def fmt_px(units, grid):
    if units is None:
        return "-"
    return f"{grid.to_price(units):.2f}" if grid is not None else str(units)


def us(total_ns: int, n: int) -> str:
    return f"{total_ns / n / 1000:.2f}us" if n else "-"


def report(raws, cfg, grace_ms: int, tf: int, last: int, perf: bool) -> None:
    with _AggTimer() as at:
        eng, cost, count = replay(raws, cfg, grace_ms, snapshot_every=100 if perf else 0)
    print(f"\nclose_grace_ms={grace_ms}")
    for inst in eng.instruments.values():
        b, ss, grid = inst.bars, inst.sessions.snapshot(), inst.grid
        if b is None:
            continue
        print(f"  instrument {inst.instrument_id} {inst.local_symbol}: bars 30s/1m/5m = "
              f"{b.completed[30]}/{b.completed[60]}/{b.completed[300]}  (open 30s: {len(b.open)})")
        bars30 = b.completed_bars(30)
        flags = Counter()
        for x in bars30:
            for f in BarFlag:
                if f and x.flags & f:
                    flags[f.name] += 1
        vol = sum(x.volume for x in bars30)
        buy, sell, unk = (sum(x.buy_volume for x in bars30), sum(x.sell_volume for x in bars30),
                          sum(x.unknown_volume for x in bars30))
        v = vol or 1
        print(f"  volume {vol}: BUY {buy} ({100 * buy / v:.1f}%)  SELL {sell} ({100 * sell / v:.1f}%)  "
              f"UNKNOWN {unk} ({100 * unk / v:.1f}%)  known_delta {buy - sell}")
        print(f"  late prints {b.late_trades} (volume {b.late_volume})  excluded {b.excluded_trades} "
              f"(volume {b.excluded_volume}) {dict(b.excluded_by_reason)}  dropped_no_price {b.dropped_no_price}")
        print("  30s flags: " + (", ".join(f"{k}={n}" for k, n in sorted(flags.items())) or "none"))
        print(f"  calendar: {'OK' if ss.calendar_ok else 'UNKNOWN (' + ss.calendar_error + ')'} tz={ss.time_zone} "
              f"anomalies={dict(ss.calendar_anomalies)}")
        for name, st in (("session", ss.session), ("rth", ss.rth), ("overnight", ss.overnight)):
            if st is not None and st.volume:
                print(f"  {name:9s} {ss.trading_date}: O {fmt_px(st.open, grid)} H {fmt_px(st.high, grid)} "
                      f"L {fmt_px(st.low, grid)} last {fmt_px(st.last, grid)} vol {st.volume} "
                      f"VWAP {grid.to_price(1) * st.vwap if grid else st.vwap:.4f}")
        if ss.session is not None:
            print(f"  session flags: observed_from_open={ss.observed_from_open} gap_observed={ss.gap_observed} "
                  f"outside_session={ss.trades_outside_session} late_session={ss.late_session_trades}")
        p = ss.previous
        print("  previous session: " + ("none observed" if p is None else
              f"{p.trading_date} H {fmt_px(p.high, grid)} L {fmt_px(p.low, grid)} C {fmt_px(p.close, grid)} "
              f"vol {p.volume} complete={p.observed_from_open and not p.gap_observed}"))
        print(f"  last {last} x {tf}s bars (UTC start, O/H/L/C, vol, B/S/U, delta, flags):")
        for x in b.completed_bars(tf)[-last:]:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(x.start_s))
            print(f"    {ts} {fmt_px(x.open, grid)}/{fmt_px(x.high, grid)}/{fmt_px(x.low, grid)}/"
                  f"{fmt_px(x.close, grid)} v={x.volume} {x.buy_volume}/{x.sell_volume}/{x.unknown_volume} "
                  f"d={x.known_delta} {x.flags.name if x.flags else ''}")
    if perf:
        _, off, offc = replay(raws, cfg, grace_ms, bars_on=False, snapshot_every=100)
        print(f"  perf (mean): trade event {us(cost['trade'], count['trade'])} with bars vs "
              f"{us(off['trade'], offc['trade'])} C4-equivalent (bars off) | clock tick "
              f"{us(cost['tick'], count['tick'])} vs {us(off['tick'], offc['tick'])} | 30s->1m "
              f"{us(at.cost[60], at.count[60])} | 1m->5m {us(at.cost[300], at.count[300])} | snapshot "
              f"{us(cost['snapshot'], count['snapshot'])} vs {us(off['snapshot'], offc['snapshot'])}")


def main(argv: list[str] | None = None) -> int:
    from tools.inspect_recording import DEFAULT_ROOT, resolve_session
    from hermes.storage.reader import iter_raw_events, verify_session
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=str(DEFAULT_ROOT))
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--grace-ms", type=int, nargs="+", default=None)
    ap.add_argument("--tf", type=int, choices=(30, 60, 300), default=60)
    ap.add_argument("--last", type=int, default=10)
    ap.add_argument("--synthetic", type=int, default=0, help="use N synthetic prints instead of a recording")
    ap.add_argument("--no-perf", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.synthetic:
        raws = synthetic_events(args.synthetic)
        print(f"Synthetic session: {args.synthetic} prints, {len(raws)} raw events")
    else:
        session = resolve_session(Path(args.path))
        if session is None:
            print(f"No recording sessions found under {args.path}", file=sys.stderr)
            return 2
        ver = verify_session(session)
        print(f"Session: {session}  (replay-complete: {'YES' if ver.replay_complete else 'NO'})")
        raws = list(iter_raw_events(session))
    for g in args.grace_ms or [cfg.bars.close_grace_ms]:
        report(raws, cfg, g, args.tf, args.last, perf=not args.no_perf)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
