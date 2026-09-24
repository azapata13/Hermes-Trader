#!/usr/bin/env python3
"""Inspect a Hermès raw recording session.

Usage:
    python tools/inspect_recording.py                     # latest session under ~/hermes-data/recordings
    python tools/inspect_recording.py <session_dir>       # a specific session
    python tools/inspect_recording.py <root_dir>          # latest session under a recordings root
    python tools/inspect_recording.py <path> --json       # machine-readable report

Exit status: 0 = replay-complete, 1 = NOT replay-complete, 2 = nothing readable.

Replay completeness is recomputed from raw ``seq`` continuity on disk; the tool never trusts
the recorder's own footer flag alone (C3 amendment E).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes.storage.reader import SessionReport, list_parts, verify_session  # noqa: E402

DEFAULT_ROOT = Path(os.path.expanduser("~/hermes-data/recordings"))


def resolve_session(path: Path) -> Path | None:
    path = path.expanduser()
    if path.is_file():
        return path.parent
    if list_parts(path):
        return path
    sessions = [p for p in path.glob("*/*") if p.is_dir() and list_parts(p)]
    if not sessions:
        return None
    return max(sessions, key=lambda p: max(f.stat().st_mtime for f in list_parts(p)))


def _ranges(r: list[tuple[int, int]]) -> str:
    return ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in r[:10]) + (" ..." if len(r) > 10 else "")


def render(rep: SessionReport) -> str:
    lines = [f"Session:        {rep.session_dir}"]
    meta = rep.meta or {}
    for key in ("hermes_version", "git_commit", "ibapi_version", "python", "normalizer_version", "contract_spec"):
        if key in meta:
            lines.append(f"  {key + ':':<20}{meta[key]}")
    lines.append(f"Parts:          {len(rep.parts)}")
    for p in rep.parts:
        state = "closed" if p.footer else "OPEN/UNCLEAN"
        extra = f", ERROR: {p.error}" if p.error else ""
        extra += f", truncated at byte {p.truncated_at}" if p.truncated_at is not None else ""
        lines.append(f"  {Path(p.path).name}: {p.raw_records} raw records, seq {p.first_seq}..{p.last_seq}, {state}{extra}")
    lines.append(f"Raw records:    {rep.raw_records}  (seq {rep.first_seq}..{rep.last_seq})")
    lines.append("By type:")
    for name, n in sorted(rep.counts_by_type.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {name:<26}{n}")
    lines.append(f"Declared gaps:  {len(rep.declared_gaps)}")
    for g in rep.declared_gaps[:10]:
        lines.append(f"  seq {g.first_seq}-{g.last_seq} ({g.count} events, {g.reason}, in {g.part})")
    lines.append(f"Missing seqs:   {_ranges(rep.missing_ranges) or 'none'}")
    lines.append(f"Undeclared:     {_ranges(rep.undeclared_ranges) or 'none'}")
    lines.append(f"Clean close:    {'yes' if rep.clean_close else 'NO'}")
    lines.append(f"Complete through seq: {rep.complete_through_seq}")
    verdict = "YES" if rep.replay_complete else "NO"
    lines.append(f"REPLAY-COMPLETE: {verdict}")
    for p in rep.problems:
        lines.append(f"  ! {p}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=str(DEFAULT_ROOT))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    session = resolve_session(Path(args.path))
    if session is None:
        print(f"No recording sessions found under {args.path}", file=sys.stderr)
        return 2
    rep = verify_session(session)
    if args.json:
        d = {k: getattr(rep, k) for k in rep.__dataclass_fields__ if k not in ("parts", "declared_gaps", "counts_by_type")}
        d["parts"] = [p.__dict__ if hasattr(p, "__dict__") else {f: getattr(p, f) for f in p.__slots__} for p in rep.parts]
        d["declared_gaps"] = [{f: getattr(g, f) for f in g.__slots__} for g in rep.declared_gaps]
        d["counts_by_type"] = dict(rep.counts_by_type)
        print(json.dumps(d, indent=2, default=str))
    else:
        print(render(rep))
    if rep.raw_records == 0:
        return 2
    return 0 if rep.replay_complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
