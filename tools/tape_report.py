#!/usr/bin/env python3
"""Replay a recording through Normalizer + MarketEngine and report C4 trade classification.

Use it to calibrate ``[tape].ambiguity_window_ms`` on real MNQ data (the 50 ms default is a
provisional C4 baseline, not an optimized value). For HISTORICAL_QUOTE classifications it also
prints the observed age distribution of the quote actually used:

    python tools/tape_report.py                              # latest session, default config
    python tools/tape_report.py <session_dir> --window-ms 0 50 100 250

For each window: trade count, BUY / SELL / UNKNOWN volume shares, counts per method and per
UNKNOWN reason. Deterministic (same recording + config => same report). Read-only.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes.config import DEFAULT_CONFIG_PATH, load_config  # noqa: E402
from hermes.ibkr.normalizer import Normalizer  # noqa: E402
from hermes.market.engine import MarketEngine  # noqa: E402
from hermes.storage.reader import iter_raw_events, verify_session  # noqa: E402
from tools.inspect_recording import DEFAULT_ROOT, resolve_session  # noqa: E402


def replay(session: Path, cfg, window_ms: int) -> dict:
    tape_cfg = dataclasses.replace(cfg.tape, ambiguity_window_ms=window_ms, max_trades=10**9, max_age_s=10**9)
    norm = Normalizer()
    eng = MarketEngine(cfg.book, cfg.session, cfg.subscriptions, tape_cfg=tape_cfg)
    for raw in iter_raw_events(session):
        for ev in norm.normalize(raw):
            eng.on_event(ev)
    trades = [t for inst in eng.instruments.values() for t in inst.tape.trades()]  # type: ignore[union-attr]
    vol = Counter()
    for t in trades:
        vol[t.aggressor.value] += t.size
    hist_ages = sorted(t.ref_quote_age_ns for t in trades
                       if t.method.value == "historical_quote" and t.ref_quote_age_ns is not None)
    return {
        "hist_ages_ns": hist_ages,
        "window_ms": window_ms, "trades": len(trades), "volume": sum(vol.values()),
        "buy": vol["buy"], "sell": vol["sell"], "unknown": vol["unknown"],
        "methods": Counter(t.method.value for t in trades),
        "unknown_reasons": Counter(t.unknown_reason.value for t in trades if t.unknown_reason),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=str(DEFAULT_ROOT))
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--window-ms", type=int, nargs="+", default=None)
    args = ap.parse_args(argv)
    session = resolve_session(Path(args.path))
    if session is None:
        print(f"No recording sessions found under {args.path}", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    ver = verify_session(session)
    print(f"Session: {session}  (replay-complete: {'YES' if ver.replay_complete else 'NO'})")
    windows = args.window_ms or [cfg.tape.ambiguity_window_ms]
    for w in windows:
        r = replay(session, cfg, w)
        v = r["volume"] or 1
        print(f"\nambiguity_window_ms={w}: {r['trades']} trades, volume {r['volume']}")
        print(f"  BUY {r['buy']} ({100 * r['buy'] / v:.1f}%)  SELL {r['sell']} ({100 * r['sell'] / v:.1f}%)  "
              f"UNKNOWN {r['unknown']} ({100 * r['unknown'] / v:.1f}%)  known_delta {r['buy'] - r['sell']}")
        print("  methods: " + ", ".join(f"{k}={n}" for k, n in sorted(r["methods"].items())))
        ages = r["hist_ages_ns"]
        if ages:
            pct = lambda q: ages[min(len(ages) - 1, int(q * len(ages)))] / 1e6  # noqa: E731
            print(f"  historical quote age ms (quote used -> trade arrival): n={len(ages)} "
                  f"p50={pct(0.5):.2f} p90={pct(0.9):.2f} p99={pct(0.99):.2f} max={ages[-1] / 1e6:.2f}")
        if r["unknown_reasons"]:
            print("  unknown: " + ", ".join(f"{k}={n}" for k, n in sorted(r["unknown_reasons"].items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
