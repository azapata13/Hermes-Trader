#!/usr/bin/env python3
"""V1 Pro missed-opportunity / shadow-setup report (read-only, no network, no order path).

    python tools/setup_report.py                                   # ~/hermes-data/setups/setups.jsonl
    python tools/setup_report.py <setups.jsonl | session_dir/setups.json> [--approvals human_approvals.jsonl]

Every number is a SHADOW measurement (hypothetical fill at the proposed entry, eligible prints only)
unless a row is marked execution == REAL, which V1 never writes. Shadow and real rows are reported
separately and never mixed. Definitions are printed with the numbers.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes.analytics.stats import OUTCOME_DEFINITION, excursion_quality, missed_report  # noqa: E402
from hermes.analytics.store import DEFAULT_STORE, load_rows  # noqa: E402


def _rows(path: Path) -> tuple[list[dict], str]:
    if path.is_dir():
        path = path / "setups.json"
    if path.suffix == ".json":
        d = json.loads(path.read_text())
        return [r for r in d.get("setups", []) if r.get("outcome", {}).get("status") != "OPEN"], str(path)
    res = load_rows(path)
    note = (f"{path} (skipped: malformed {res.malformed}, unsupported schema {res.unsupported_schema}, "
            f"duplicates {res.duplicates}, unfinished {res.unfinished})")
    return res.rows, note


def _approvals(path: Path | None) -> list[dict]:
    if path is None or not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", default=DEFAULT_STORE)
    ap.add_argument("--approvals", default="~/hermes-data/logs/human_approvals.jsonl")
    ap.add_argument("--top", type=int, default=10, help="best shadow setups by excursion quality")
    args = ap.parse_args(argv)
    rows, note = _rows(Path(args.source).expanduser())
    approvals = _approvals(Path(args.approvals).expanduser() if args.approvals else None)
    rep = missed_report(rows, approvals)
    print("=========== HERMÈS SETUP REPORT (shadow measurements — never P&L, no order was sent) ===========")
    print(f"source           {note}")
    print(f"approvals        {len(approvals)} journal entries")
    print(f"definition       {OUTCOME_DEFINITION}")
    for line in rep.lines():
        print(line)
    best = sorted(((excursion_quality(r), r) for r in rows if excursion_quality(r) is not None),
                  key=lambda x: (-x[0], x[1]["setup_id"]))[: args.top]
    if best:
        print("best shadow setups by excursion quality = MFE / (MAE + 1 tick) (quality, not profit):")
        for q, r in best:
            o = r["outcome"]
            upp = r.get("units_per_point") or 4
            print(f"  {r['setup_id']} {r['direction']:5s} quality {q:5.1f}  MFE {o['mfe_units'] / upp:6.2f} pt  "
                  f"MAE {o['mae_units'] / upp:5.2f} pt  window {r.get('window_close_status') or '-'}  "
                  f"re-armed {'yes' if r.get('rearmed_by') else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
