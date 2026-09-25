#!/usr/bin/env python3
"""Deterministic replay of a Hermès raw recording (C6). Never connects to TWS.

    python tools/replay_report.py                                   # latest session under ~/hermes-data/recordings
    python tools/replay_report.py <session_dir|date_dir|root> --verify
    python tools/replay_report.py <path> --mode paced --speed 10     # visual/debug pacing; same results
    python tools/replay_report.py <path> --save a.json               # persist the checkpoint sequence
    python tools/replay_report.py <path> --compare a.json            # compare with a saved replay / live sidecar

Prints: session, versions, integrity (complete / gaps / truncated / clean close), elapsed and
throughput, final state hash, book state, classified trades and BUY/SELL/UNKNOWN volume, 30 s / 1 m /
5 m bar counts, session VWAP/H/L, checkpoints, and the live-vs-replay equivalence verdict when the
session carries live checkpoints (``checkpoints.json``, written by C6+ live runs).

Exit status: 0 OK; 1 incompatible recording or a verify/compare mismatch; 2 nothing to replay.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes.replay.checkpoints import CheckpointPolicy, compare_checkpoints, load_checkpoints  # noqa: E402
from hermes.replay.runner import ReplayMode, ReplayOptions, replay_session  # noqa: E402
from hermes.replay.source import ReplayIncompatible  # noqa: E402

DEFAULT_ROOT = Path(os.path.expanduser("~/hermes-data/recordings"))


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


def _px(units, grid) -> str:
    if units is None:
        return "-"
    return f"{grid.to_price(units):.2f}" if grid is not None else str(units)


def render(r) -> list[str]:
    ig, m = r.integrity, r.info.meta
    grid = None
    if r.engine is not None and r.engine.instruments:
        grid = next(iter(r.engine.instruments.values())).grid
    v = r.buy_volume + r.sell_volume + r.unknown_volume or 1
    lines = [
        f"session     {r.session_dir}",
        f"recorded    hermes {m.get('hermes_version', '?')} git {m.get('git_commit', '?')} ibapi {m.get('ibapi_version', '?')} "
        f"python {m.get('python', '?')} schema v{r.info.schema_version} normalizer v{r.info.normalizer_version}",
        f"replayed    code {r.code_fingerprint} config {r.config_fingerprint} ({r.config_source}) mode {r.mode}"
        + (f" x{r.speed:g}" if r.mode == "paced" else ""),
        f"records     {r.raw_events} raw (seq {r.first_seq}..{r.final_seq}) -> {r.normalized_events} market events"
        f"  raw_digest {ig.raw_digest[:16]}",
        f"integrity   {ig.label}",
    ]
    for p in ig.problems():
        lines.append(f"            - {p}")
    lines += [
        f"elapsed     {r.elapsed_s:.3f}s  {r.raw_per_s:,.0f} raw/s  {r.events_per_s:,.0f} events/s  "
        f"(checkpoint hashing {r.checkpoint_s * 1000:.1f} ms, peak RSS {r.peak_rss_mb:.0f} MB)",
        f"state hash  {r.final_hash}",
        f"health      market_data_ok={r.market_data_ok} book={r.book_state}"
        + ("" if r.market_data_ok else f" reasons={list(r.not_ok_reasons)[:4]}"),
        f"trades      {r.classified_trades} classified  BUY {r.buy_volume} ({100 * r.buy_volume / v:.1f}%)  "
        f"SELL {r.sell_volume} ({100 * r.sell_volume / v:.1f}%)  UNKNOWN {r.unknown_volume} ({100 * r.unknown_volume / v:.1f}%)",
        f"bars        30s/1m/5m = {r.bars[0]}/{r.bars[1]}/{r.bars[2]}",
    ]
    s = r.session
    if s is None or not s.calendar_ok:
        lines.append(f"session     unavailable ({s.calendar_error if s else 'no instrument'})")
    else:
        st = s.session
        if st is not None and st.volume:
            vwap = grid.to_price(1) * st.vwap_num / st.volume if grid else st.vwap_num / st.volume
            lines.append(f"session     {s.trading_date} VWAP {vwap:.2f} H {_px(st.high, grid)} L {_px(st.low, grid)} "
                         f"vol {st.volume} observed_from_open={s.observed_from_open} gap={s.gap_observed}")
        else:
            lines.append(f"session     {s.trading_date or 'closed'} (no bar-eligible prints in the current session)")
    lines.append(f"checkpoints {r.checkpoint_count} + final  (policy every_n={r.policy.every_n}, bar_close, health)")
    lines.append(f"live        {r.live_compare_status}")
    for n in r.notes:
        lines.append(f"note        {n}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=None)
    ap.add_argument("--session", default=None, help="same as the positional path")
    ap.add_argument("--mode", choices=("fast", "paced"), default="fast")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--max-sleep", type=float, default=None, help="paced: cap idle gaps (seconds)")
    ap.add_argument("--config", default="recorded", help="recorded | current | <path to toml>")
    ap.add_argument("--every", type=int, default=None, help="checkpoint every N raw seqs (default: live policy or 10000)")
    ap.add_argument("--stop-at-gap", action="store_true", help="replay only the deterministic prefix")
    ap.add_argument("--verify", action="store_true", help="replay twice and require identical checkpoints")
    ap.add_argument("--save", default=None, help="write this replay's checkpoints as JSON")
    ap.add_argument("--compare", default=None, help="compare with a saved replay JSON or a live checkpoints.json")
    args = ap.parse_args(argv)

    session = find_session(Path(args.session or args.path or DEFAULT_ROOT))
    if session is None:
        print(f"No recording found under {args.session or args.path or DEFAULT_ROOT}", file=sys.stderr)
        return 2
    policy = CheckpointPolicy(every_n=args.every) if args.every is not None else None
    opts = ReplayOptions(mode=ReplayMode(args.mode), speed=args.speed, config=args.config, policy=policy,
                         stop_at_gap=args.stop_at_gap, max_sleep_s=args.max_sleep)
    try:
        r = replay_session(session, opts)
    except ReplayIncompatible as exc:
        print(f"INCOMPATIBLE RECORDING: {exc}", file=sys.stderr)
        return 1
    print("\n".join(render(r)))
    status = 0
    if args.verify:
        r2 = replay_session(session, ReplayOptions(config=args.config, policy=r.policy, stop_at_gap=args.stop_at_gap))
        same = ([c.hash for c in r2.checkpoints] == [c.hash for c in r.checkpoints]
                and [c.key() for c in r2.checkpoints] == [c.key() for c in r.checkpoints]
                and r2.final_hash == r.final_hash and r2.integrity.raw_digest == r.integrity.raw_digest)
        print(f"verify      {'IDENTICAL' if same else 'DIFFERENT'} on second FAST replay "
              f"({len(r2.checkpoints)} checkpoints + final)")
        status |= 0 if same else 1
    if args.compare:
        other = load_checkpoints(Path(args.compare))
        cmp = compare_checkpoints(other.checkpoints, r.checkpoints, other.final, r.final)
        code_same = other.meta.get("code_fingerprint") == r.code_fingerprint
        print(f"compare     {'EQUIVALENT' if cmp.equivalent else 'MISMATCH'} {cmp.matched}/{cmp.compared} checkpoints "
              f"(only-a {cmp.only_a}, only-b {cmp.only_b}); first mismatch {cmp.first_mismatch}"
              + ("" if code_same else "  [different code fingerprint: informational only]"))
        status |= 0 if cmp.equivalent else 1
    if args.save:
        Path(args.save).write_text(json.dumps(r.to_dict(), separators=(",", ":")))
        print(f"saved       {args.save}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
