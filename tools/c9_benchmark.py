#!/usr/bin/env python3
"""C9f performance benchmark: market engine, snapshots, decision layer, checkpoints (never connects).

    python tools/c9_benchmark.py                          # latest real recording under ~/hermes-data/recordings
    python tools/c9_benchmark.py <session_dir>
    python tools/c9_benchmark.py --synthetic 5            # 5 minutes of a busy SYNTHETIC MNQ-like market
    python tools/c9_benchmark.py <path> --json out.json

Measures separately (median / p99 / max in microseconds):
  replay throughput (decisions ON and OFF), MarketEngine processing per raw event type, engine.snapshot(),
  C7 metrics / C8 structure / C8 patterns snapshots, DecisionContext, candidate evaluation, SafetyPolicy
  (facts + evaluate), lifecycle event check (a synthetic ACTIONABLE candidate on the REAL market state),
  market state hash, decision fingerprint / checkpoint, peak RSS.

Snapshot-type costs are sampled at a fixed EVENT-time cadence (``--sample-ms``, default 100 ms = the live
snapshot publication cadence), so the numbers reflect the state sizes the live runtime actually sees.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import random
import resource
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes.decision.candidate import Direction, evaluate_candidate  # noqa: E402
from hermes.decision.context import build_decision_context  # noqa: E402
from hermes.decision.lifecycle import LifecycleTracker  # noqa: E402
from hermes.decision.runtime import DecisionRuntime  # noqa: E402
from hermes.decision.safety import PURPOSE_LIFECYCLE, SafetyPolicy, facts_from_engine  # noqa: E402
from hermes.ibkr.normalizer import Normalizer  # noqa: E402
from hermes.market.engine import MarketEngine  # noqa: E402
from hermes.market.events import BookSide  # noqa: E402
from hermes.replay import fingerprint as fp  # noqa: E402
from hermes.replay.runner import ReplayOptions, replay_session, resolve_config  # noqa: E402
from hermes.replay.source import RecordingSource  # noqa: E402

DEFAULT_ROOT = Path(os.path.expanduser("~/hermes-data/recordings"))
_now = time.perf_counter_ns


def find_session(path: Path) -> Path | None:
    path = path.expanduser()
    if any(path.glob("part-*.hrec")):
        return path
    dirs = {p.parent for p in path.rglob("part-*.hrec")}
    return max(dirs, key=lambda d: max(f.stat().st_mtime for f in d.glob("part-*.hrec"))) if dirs else None


# ---------------------------------------------------------------------------- synthetic market

def synthetic_session(minutes: float, out: Path, seed: int = 7) -> Path:
    """Busy, MNQ-like SYNTHETIC recording: ~150 depth + ~15 BBO + ~6 trade callbacks per second,
    10-row book, random walk; timer ticks every 250 ms. Deterministic for a given seed."""
    from tests.support import BBO, DEPTH, TICK, TRADES, RawScript, write_hrec
    from tests.unit.test_replay import WEEK
    rnd = random.Random(seed)
    t0 = 1790085600                                  # RTH
    sc = RawScript(wall0=t0 * 10**9)
    sc.bootstrap(**WEEK)
    bid = 21000.0
    rows = 10
    sizes = {BookSide.BID: [10 + i for i in range(rows)], BookSide.ASK: [10 + i for i in range(rows)]}
    for i in range(rows):
        sc.depth(DEPTH, i, 0, 1, bid - TICK * i, sizes[BookSide.BID][i])
        sc.depth(DEPTH, i, 0, 0, bid + TICK * (i + 1), sizes[BookSide.ASK][i])
    sc.bbo(BBO, bid, bid + TICK)
    sc.mdt(10_004, 1)
    step_ms, t_ms, next_tick = 5.0, 0.0, 250.0
    end_ms = minutes * 60_000
    while t_ms < end_ms:
        t_ms += step_ms
        sc.advance(step_ms)
        if t_ms >= next_tick:
            sc.tick()
            next_tick += 250.0
        u = rnd.random()
        if u < 0.75:                                 # depth size update
            side = BookSide.BID if rnd.random() < 0.5 else BookSide.ASK
            i = min(rows - 1, int(rnd.expovariate(0.4)))
            sizes[side][i] = max(1, sizes[side][i] + rnd.choice((-3, -2, -1, 1, 2, 3)))
            price = bid - TICK * i if side is BookSide.BID else bid + TICK * (i + 1)
            sc.depth(DEPTH, i, 1, 1 if side is BookSide.BID else 0, price, sizes[side][i])
        elif u < 0.83:                               # BBO refresh
            sc.bbo(BBO, bid, bid + TICK, sizes[BookSide.BID][0], sizes[BookSide.ASK][0])
        elif u < 0.86:                               # trade at the touch
            if rnd.random() < 0.5:
                sc.trade(TRADES, bid + TICK, rnd.randint(1, 5))
            else:
                sc.trade(TRADES, bid, rnd.randint(1, 5))
        elif u < 0.868:                              # price step: shift the whole book one tick
            bid += TICK if rnd.random() < 0.5 else -TICK
            for i in range(rows):
                sc.depth(DEPTH, i, 1, 0, bid + TICK * (i + 1), sizes[BookSide.ASK][i])
            for i in range(rows):
                sc.depth(DEPTH, i, 1, 1, bid - TICK * i, sizes[BookSide.BID][i])
            sc.bbo(BBO, bid, bid + TICK, sizes[BookSide.BID][0], sizes[BookSide.ASK][0])
    return write_hrec(out, sc.events, session_id=f"synthetic-{minutes:g}m-seed{seed}")


# ---------------------------------------------------------------------------- measurement helpers

class Stat:
    def __init__(self) -> None:
        self.v: list[int] = []

    def add(self, ns: int) -> None:
        self.v.append(ns)

    def row(self) -> dict:
        if not self.v:
            return {"n": 0}
        s = sorted(self.v)
        p = lambda q: s[min(len(s) - 1, int(q * len(s)))] / 1000  # noqa: E731
        return {"n": len(s), "median_us": round(statistics.median(s) / 1000, 1), "p99_us": round(p(0.99), 1),
                "max_us": round(s[-1] / 1000, 1), "mean_us": round(sum(s) / len(s) / 1000, 1)}


def timed(stat: Stat, fn, *a, **k):
    t = _now()
    out = fn(*a, **k)
    stat.add(_now() - t)
    return out


def peak_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


def component_pass(session: Path, sample_ms: int) -> dict:
    src = RecordingSource(session)
    cfg, _, _ = resolve_config(src.info, "recorded")
    raws = list(src.events())
    norm = Normalizer()
    eng = MarketEngine(cfg.book, cfg.session, cfg.subscriptions, tape_cfg=cfg.tape, bars_cfg=cfg.bars)
    drt = DecisionRuntime(eng, cfg.decision)
    policy = SafetyPolicy(cfg.decision)
    scratch = LifecycleTracker(cfg.decision)
    by_type: dict[str, Stat] = {}
    S = {k: Stat() for k in ("engine_snapshot", "metrics_snapshot", "structure_snapshot", "patterns_snapshot",
                             "decision_context", "candidate_evaluation", "safety_facts", "safety_evaluate",
                             "lifecycle_check_synthetic_actionable", "decision_runtime_per_event",
                             "market_state_hash", "decision_fingerprint")}
    next_sample = None
    last_cand = None
    for raw in raws:
        t = _now()
        for ev in norm.normalize(raw):
            eng.on_event(ev)
        by_type.setdefault(type(raw).__name__, Stat()).add(_now() - t)
        n_before = len(drt.journal)
        timed(S["decision_runtime_per_event"], drt.after_event, raw, (), raw.recv_mono_ns)
        if len(drt.journal) != n_before:
            timed(S["decision_fingerprint"], drt.fingerprint)
        m = raw.recv_mono_ns
        if next_sample is None:
            next_sample = m
        if m < next_sample or not eng.instruments:
            continue
        next_sample = m + sample_ms * 1_000_000
        inst = next(iter(eng.instruments.values()))
        snap = timed(S["engine_snapshot"], eng.snapshot)
        timed(S["metrics_snapshot"], inst.metrics.snapshot)
        timed(S["structure_snapshot"], inst.structure.snapshot)
        timed(S["patterns_snapshot"], inst.patterns.snapshot)
        ctx = timed(S["decision_context"], build_decision_context, snap, cfg.decision)
        if ctx is not None:
            last_cand = timed(S["candidate_evaluation"], evaluate_candidate, ctx, cfg.decision)
        facts = timed(S["safety_facts"], facts_from_engine, eng, inst, raw.recv_wall_ns)
        timed(S["safety_evaluate"], policy.evaluate, facts, purpose=PURPOSE_LIFECYCLE,
              baseline=last_cand)
        if last_cand is not None and inst.book is not None:
            bids, asks = inst.book.levels(BookSide.BID), inst.book.levels(BookSide.ASK)
            if bids and asks:
                c = dataclasses.replace(last_cand, direction=Direction.LONG, entry_reference=asks[0][0],
                                        structural_invalidation=bids[0][0] - 40, proposed_stop=asks[0][0] - 40,
                                        trigger_bar_end_s=raw.recv_wall_ns // 10**9)
                scratch.active.clear()
                scratch.register(c)                  # direction LONG -> ACTIONABLE record (synthetic)
                timed(S["lifecycle_check_synthetic_actionable"], scratch.update, eng, raw.seq, raw.recv_wall_ns)
        timed(S["market_state_hash"], fp.state_hash, eng)
    out = {k: v.row() for k, v in S.items()}
    out["engine_per_raw_type"] = {k: v.row() for k, v in sorted(by_type.items())}
    allv = Stat()
    for v in by_type.values():
        allv.v.extend(v.v)
    out["engine_per_raw_event"] = allv.row()
    out["decision_counts"] = drt.counts()
    return out


def replay_pass(session: Path, decisions: bool) -> dict:
    r = replay_session(session, ReplayOptions(decisions=decisions, compare_live=False))
    return {"raw_events": r.raw_events, "elapsed_s": round(r.elapsed_s, 3), "raw_per_s": round(r.raw_per_s),
            "checkpoints": len(r.checkpoints), "checkpoint_hashing_s": round(r.checkpoint_s, 3),
            "final_hash": r.final_hash,
            "decision_final": r.decisions.final.fingerprint if r.decisions is not None else None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=None)
    ap.add_argument("--synthetic", type=float, default=None, help="minutes of synthetic busy market")
    ap.add_argument("--sample-ms", type=int, default=100)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    if args.synthetic:
        session = synthetic_session(args.synthetic, Path(tempfile.mkdtemp()) / "synthetic")
        kind = f"SYNTHETIC ({args.synthetic:g} min busy MNQ-like market, generated)"
    else:
        session = find_session(Path(args.path) if args.path else DEFAULT_ROOT)
        if session is None:
            print("no recording found", file=sys.stderr)
            return 2
        sid = RecordingSource(session).info.session_id
        kind = "SYNTHETIC recording" if str(sid).startswith("synthetic") else "REAL recording"
    res: dict = {"source": kind, "session": str(session)}
    on = [replay_pass(session, True) for _ in range(args.rounds)]
    off = [replay_pass(session, False) for _ in range(args.rounds)]
    res["replay_decisions_on"] = max(on, key=lambda x: x["raw_per_s"])
    res["replay_decisions_off"] = max(off, key=lambda x: x["raw_per_s"])
    res["replay_identical"] = len({(x["final_hash"], x["decision_final"]) for x in on}) == 1
    res["components"] = component_pass(session, args.sample_ms)
    res["peak_rss_mb"] = round(peak_rss_mb(), 1)
    print(f"source      {kind}\nsession     {session}")
    for k in ("replay_decisions_on", "replay_decisions_off"):
        x = res[k]
        print(f"{k:34s} {x['raw_events']} raw in {x['elapsed_s']}s = {x['raw_per_s']:,} raw/s "
              f"(checkpoint hashing {x['checkpoint_hashing_s']}s over {x['checkpoints']} checkpoints)")
    print(f"{'replays identical':34s} {res['replay_identical']}")
    for k, v in res["components"].items():
        if k in ("engine_per_raw_type", "decision_counts"):
            continue
        print(f"{k:34s} {v}")
    for k, v in res["components"]["engine_per_raw_type"].items():
        print(f"  engine {k:27s} {v}")
    print(f"{'decision counts':34s} {res['components']['decision_counts']}")
    print(f"{'peak RSS':34s} {res['peak_rss_mb']} MB")
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
