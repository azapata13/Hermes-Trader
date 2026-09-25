#!/usr/bin/env python3
"""C7 performance benchmark on a Hermès recording.

Measures:
- MarketEngine dispatch latency by event type (DEPTH / BBO / TRADE)
- end-to-end raw replay throughput with C7 metrics ON
- a shadow baseline with the same C7 code but MetricsEngine replaced by no-op methods
- approximate peak bytes retained by C7 metrics state (sampled, not allocator RSS)

The shadow baseline is NOT the historical C6 binary. It isolates the incremental
cost of C7 metric calculations inside the current codebase.

Never connects to TWS and never mutates a recording.

Usage:
    python tools/metrics_benchmark.py
    python tools/metrics_benchmark.py <session_dir|date_dir|recordings_root>
    python tools/metrics_benchmark.py <path> --rounds 3
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import statistics
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes.ibkr.normalizer import Normalizer  # noqa: E402
from hermes.market import engine as engine_mod  # noqa: E402
from hermes.market import events as M  # noqa: E402
from hermes.replay.runner import resolve_config  # noqa: E402
from hermes.replay.source import RecordingSource, ReplayIncompatible  # noqa: E402

DEFAULT_ROOT = Path(os.path.expanduser("~/hermes-data/recordings"))


class NullMetrics:
    """No-op stand-in used only for the shadow performance baseline."""

    __slots__ = ("instrument_id",)

    def __init__(self, instrument_id: int) -> None:
        self.instrument_id = instrument_id

    def observe_book(self, *a, **k) -> None:
        return None

    def on_bbo(self, *a, **k) -> None:
        return None

    def on_trade(self, *a, **k) -> None:
        return None

    def advance(self, *a, **k) -> None:
        return None

    def break_book(self, *a, **k) -> None:
        return None

    def break_trade(self, *a, **k) -> None:
        return None

    def break_all(self, *a, **k) -> None:
        return None

    def token(self) -> tuple:
        return ()

    def fingerprint_state(self) -> tuple:
        return ()

    def snapshot(self):
        return None


def find_session(path: Path) -> Path | None:
    path = path.expanduser()
    if path.is_file():
        return path.parent
    if any(path.glob("part-*.hrec")):
        return path
    dirs = {p.parent for p in path.rglob("part-*.hrec")}
    if not dirs:
        return None
    return max(dirs, key=lambda d: max(f.stat().st_mtime for f in d.glob("part-*.hrec")))


def _pct(values: list[int], q: float) -> float:
    if not values:
        return 0.0
    a = sorted(values)
    idx = min(len(a) - 1, max(0, int((len(a) - 1) * q)))
    return a[idx] / 1000.0


def _deep_size(obj: Any, seen: set[int] | None = None) -> int:
    """Approximate retained Python object bytes; excludes interpreter/allocator overhead not reachable here."""
    if obj is None:
        return 0
    if seen is None:
        seen = set()
    oid = id(obj)
    if oid in seen:
        return 0
    seen.add(oid)

    n = sys.getsizeof(obj)
    if dataclasses.is_dataclass(obj):
        for f in dataclasses.fields(obj):
            n += _deep_size(getattr(obj, f.name), seen)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            n += _deep_size(k, seen) + _deep_size(v, seen)
    elif isinstance(obj, (tuple, list, deque, set, frozenset)):
        for x in obj:
            n += _deep_size(x, seen)
    return n


def _metrics_bytes(engine) -> int:
    total = 0
    attrs = (
        "_book_snapshot", "_prev_l1", "_event_ofi", "_ofi_events", "_trades",
        "_depth_events", "_bbo_events", "_midpoints", "_last_prices",
    )
    for inst in engine.instruments.values():
        m = inst.metrics
        seen: set[int] = set()
        total += sys.getsizeof(m)
        for name in attrs:
            if hasattr(m, name):
                total += _deep_size(getattr(m, name), seen)
    return total


def _make_engine(cfg, metrics_on: bool):
    original = engine_mod.MetricsEngine
    try:
        if not metrics_on:
            engine_mod.MetricsEngine = NullMetrics
        return engine_mod.MarketEngine(
            cfg.book, cfg.session, cfg.subscriptions,
            tape_cfg=cfg.tape, bars_cfg=cfg.bars,
        )
    finally:
        engine_mod.MetricsEngine = original


def run_once(raws: list[Any], cfg, metrics_on: bool, collect_latency: bool = False,
             sample_memory: bool = False) -> dict[str, Any]:
    norm = Normalizer()
    eng = _make_engine(cfg, metrics_on)
    lat: dict[str, list[int]] = {"depth": [], "bbo": [], "trade": []}
    normalized = 0
    peak_metrics_bytes = 0

    t0 = time.perf_counter_ns()
    for i, raw in enumerate(raws, 1):
        try:
            events = norm.normalize(raw)
            normalized += len(events)
            for ev in events:
                kind = None
                if type(ev) is M.DepthRowEvent:
                    kind = "depth"
                elif type(ev) is M.BboEvent:
                    kind = "bbo"
                elif type(ev) is M.TradeEvent:
                    kind = "trade"

                if collect_latency and kind is not None:
                    a = time.perf_counter_ns()
                    eng.on_event(ev)
                    lat[kind].append(time.perf_counter_ns() - a)
                else:
                    eng.on_event(ev)
        except Exception as exc:  # mirror live/replay fail-safe
            eng.internal_error(f"{type(raw).__name__} seq={raw.seq}: {exc!r}", raw.recv_mono_ns)

        if sample_memory and metrics_on and (i % 100 == 0 or i == len(raws)):
            peak_metrics_bytes = max(peak_metrics_bytes, _metrics_bytes(eng))

    elapsed_ns = time.perf_counter_ns() - t0
    elapsed_s = elapsed_ns / 1e9
    return {
        "elapsed_s": elapsed_s,
        "raw_per_s": len(raws) / elapsed_s if elapsed_s else 0.0,
        "events_per_s": normalized / elapsed_s if elapsed_s else 0.0,
        "lat": lat,
        "peak_metrics_bytes": peak_metrics_bytes,
        "engine": eng,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=str(DEFAULT_ROOT))
    ap.add_argument("--config", default="recorded", help="recorded | current | <path to toml>")
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args(argv)

    if args.rounds < 1:
        print("--rounds must be >= 1", file=sys.stderr)
        return 2

    session = find_session(Path(args.path))
    if session is None:
        print(f"No recording found under {args.path}", file=sys.stderr)
        return 2

    try:
        src = RecordingSource(session)
        cfg, cfg_source, notes = resolve_config(src.info, args.config)
        raws = list(src.events())
    except ReplayIncompatible as exc:
        print(f"INCOMPATIBLE RECORDING: {exc}", file=sys.stderr)
        return 1

    # Warm both paths once; not reported.
    run_once(raws, cfg, False)
    run_once(raws, cfg, True)

    off_rates: list[float] = []
    on_rates: list[float] = []
    lat_all = {"depth": [], "bbo": [], "trade": []}

    # Alternate order each round to reduce systematic thermal/cache bias.
    for r in range(args.rounds):
        order = (False, True) if r % 2 == 0 else (True, False)
        for enabled in order:
            out = run_once(raws, cfg, enabled, collect_latency=enabled)
            if enabled:
                on_rates.append(out["raw_per_s"])
                for k in lat_all:
                    lat_all[k].extend(out["lat"][k])
            else:
                off_rates.append(out["raw_per_s"])

    mem = run_once(raws, cfg, True, sample_memory=True)["peak_metrics_bytes"]

    off = statistics.median(off_rates)
    on = statistics.median(on_rates)
    overhead = 0.0 if off == 0 else 100.0 * (off - on) / off

    print(f"session      {session}")
    print(f"config       {cfg_source}")
    print(f"records      {len(raws)} raw")
    print(f"rounds       {args.rounds} (+ warmup)")
    print("baseline     shadow only: current C7 code with metrics methods no-op; NOT historical C6")
    print(f"throughput   metrics OFF {off:,.0f} raw/s")
    print(f"             metrics ON  {on:,.0f} raw/s")
    print(f"             C7 overhead {overhead:+.1f}% vs shadow baseline")
    print("dispatch latency (C7 ON)")
    for k in ("depth", "bbo", "trade"):
        vals = lat_all[k]
        print(f"  {k:<7}    n={len(vals):>7}  median={_pct(vals, 0.50):7.2f} us  p99={_pct(vals, 0.99):7.2f} us")
    print(f"memory       sampled peak C7 metric state ~= {mem / 1024:.1f} KiB")
    for n in notes:
        print(f"note         {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
