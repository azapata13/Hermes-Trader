#!/usr/bin/env python3
"""C9 final acceptance: fresh live MNQ run (READ-ONLY) + live-vs-replay + second replay + performance.

    python tools/c9_acceptance.py --live 60          # record ~60 s live (TWS must be running), then validate
    python tools/c9_acceptance.py [<session_dir>]     # validate an existing recording (latest by default)

Live part: the standard ``LiveRuntime`` (ReadOnlyClient, no order path, orders_enabled=false) records the
raw stream, market checkpoints and the decision journal / decision checkpoints, then prints its summary.
Validation part (never connects):
  1. replay #1 vs the live sidecars: MARKET EQUIVALENT (all checkpoints + final hash) and
     DECISION EQUIVALENT (every journal record, decision checkpoint and the final decision fingerprint);
  2. replay #2 (FAST) vs replay #1: IDENTICAL market and decision outputs;
  3. performance on this REAL recording (tools/c9_benchmark component pass).
A JSON report is written to ``<session_dir>/c9_acceptance.json``. Exit 0 only if every check passes.

NONE-only decisions are valid: the run validates deterministic behavior, not that a setup occurred.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes.config import DEFAULT_CONFIG_PATH, load_config  # noqa: E402
from hermes.replay.decisions import compare_decisions  # noqa: E402
from hermes.replay.runner import ReplayOptions, replay_session  # noqa: E402

DEFAULT_ROOT = Path(os.path.expanduser("~/hermes-data/recordings"))


def find_session(path: Path) -> Path | None:
    path = path.expanduser()
    if any(path.glob("part-*.hrec")):
        return path
    dirs = {p.parent for p in path.rglob("part-*.hrec")}
    return max(dirs, key=lambda d: max(f.stat().st_mtime for f in d.glob("part-*.hrec"))) if dirs else None


def run_live(config: str, duration: float) -> tuple[dict, Path]:
    from hermes.app.run_live import LiveRuntime
    from hermes.core.logging_setup import setup_logging
    cfg = load_config(config)
    assert not cfg.safety.orders_enabled and cfg.ibkr.read_only, "READ-ONLY configuration required"
    handle = setup_logging(cfg.telemetry.log_directory, console=cfg.telemetry.console)
    try:
        rt = LiveRuntime(cfg, record=True)
        summary = rt.run(duration)
    finally:
        handle.stop()
    if rt.recorder is None:
        raise SystemExit("recorder disabled: cannot validate")
    return summary, Path(rt.recorder.session_dir)


def validate(session: Path) -> dict:
    a = replay_session(session)
    b = replay_session(session, ReplayOptions(policy=a.policy))
    market_identical = ([(c.key(), c.hash) for c in a.checkpoints] == [(c.key(), c.hash) for c in b.checkpoints]
                        and a.final_hash == b.final_hash and a.integrity.raw_digest == b.integrity.raw_digest)
    dec_identical = (a.decisions is not None and b.decisions is not None
                     and compare_decisions(a.decision_set, b.decision_set).equivalent
                     and a.decisions.final == b.decisions.final)
    lc, dc = a.live_compare, a.decision_compare
    return {
        "session": str(session), "raw_events": a.raw_events, "replay_complete": a.integrity.replay_complete,
        "integrity": a.integrity.label,
        "market": {"live_status": a.live_compare_status,
                   "equivalent": bool(lc and lc.equivalent),
                   "checkpoints": f"{lc.matched}/{lc.compared}" if lc else None,
                   "final_hash_match": bool(lc and lc.final_match), "final_hash": a.final_hash,
                   "second_replay_identical": market_identical},
        "decision": {"live_status": a.decision_compare_status,
                     "equivalent": bool(dc and dc.equivalent),
                     "detail": dc.lines() if dc else None,
                     "counts": a.decisions.counts() if a.decisions else None,
                     "final_fingerprint": a.decisions.final.fingerprint if a.decisions and a.decisions.final else None,
                     "second_replay_identical": dec_identical},
        "replay_throughput_raw_per_s": round(max(a.raw_per_s, b.raw_per_s)),
        "checkpoint_hashing_s": round(a.checkpoint_s, 3), "peak_rss_mb": round(a.peak_rss_mb, 1),
        "recorded_meta": {k: a.info.meta.get(k) for k in ("hermes_version", "git_commit", "code_fingerprint",
                                                          "ibapi_version", "python", "platform")},
        "replay_code_fingerprint": a.code_fingerprint,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=None)
    ap.add_argument("--live", type=float, default=None, help="record N seconds live first (TWS required)")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--no-bench", action="store_true")
    args = ap.parse_args(argv)
    report: dict = {}
    if args.live:
        summary, session = run_live(args.config, args.live)
        report["live_summary"] = summary
        print("\n=========== LIVE SUMMARY ===========")
        for k, v in summary.items():
            print(f"{k:32s} {v}")
    else:
        session = find_session(Path(args.path) if args.path else DEFAULT_ROOT)
        if session is None:
            print("no recording found", file=sys.stderr)
            return 2
    v = validate(session)
    report["validation"] = v
    if not args.no_bench:
        from tools.c9_benchmark import component_pass
        report["performance_real_recording"] = component_pass(session, 100)

    live_ok = None                                  # not part of this validation (existing recording)
    if "live_summary" in report:
        s = report["live_summary"]
        live_ok = bool(s["healthy"] and s["live_data_confirmed"] and s["read_only_violations"] == 0
                       and s["replay_complete"])
    checks = {
        "live run healthy (connection, contract, LIVE data, read-only, replay-complete)": live_ok,
        "MARKET live vs replay EQUIVALENT (all checkpoints + final hash)": v["market"]["equivalent"]
        and v["market"]["final_hash_match"],
        "DECISION live vs replay EQUIVALENT (journal, checkpoints, final fingerprint)": v["decision"]["equivalent"],
        "second FAST replay IDENTICAL (market)": v["market"]["second_replay_identical"],
        "second FAST replay IDENTICAL (decision)": v["decision"]["second_replay_identical"],
    }
    report["checks"] = checks
    report["accepted"] = all(v for v in checks.values() if v is not None)
    print("\n=========== C9 ACCEPTANCE ===========")
    print(f"session      {v['session']}  ({v['raw_events']} raw, {v['integrity']})")
    print(f"MARKET       {v['market']['live_status']}")
    print(f"             second replay {'IDENTICAL' if v['market']['second_replay_identical'] else 'DIFFERENT'}"
          f"  final hash {v['market']['final_hash']}")
    print(f"DECISION     {v['decision']['live_status']}")
    for line in v["decision"]["detail"] or []:
        print(f"             {line}")
    print(f"             second replay {'IDENTICAL' if v['decision']['second_replay_identical'] else 'DIFFERENT'}"
          f"  final fingerprint {v['decision']['final_fingerprint']}")
    print(f"             counts {v['decision']['counts']}")
    print(f"replay       {v['replay_throughput_raw_per_s']:,} raw/s  checkpoint hashing {v['checkpoint_hashing_s']}s"
          f"  peak RSS {v['peak_rss_mb']} MB")
    perf = report.get("performance_real_recording")
    if perf:
        for k in ("engine_per_raw_event", "engine_snapshot", "metrics_snapshot", "structure_snapshot",
                  "patterns_snapshot", "decision_context", "candidate_evaluation", "safety_facts", "safety_evaluate",
                  "lifecycle_check_synthetic_actionable", "decision_runtime_per_event", "market_state_hash",
                  "decision_fingerprint"):
            print(f"perf         {k:38s} {perf[k]}")
    for name, ok in checks.items():
        print(f"{'SKIP' if ok is None else 'PASS' if ok else 'FAIL'}         {name}")
    print(f"RESULT       {'C9 ACCEPTANCE CHECKS PASSED' if report['accepted'] else 'NOT ACCEPTED'}")
    out = session / "c9_acceptance.json"
    out.write_text(json.dumps(report, indent=1, default=lambda o: dataclasses.asdict(o) if dataclasses.is_dataclass(o) else str(o)))
    print(f"report       {out}")
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
