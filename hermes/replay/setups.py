"""V1 Pro setup-tracking persistence (``setups.json``) and live-vs-replay comparison.

A live run writes ``<session_dir>/setups.json`` once at shutdown (never I/O on the dispatch thread):
the tracking journal (every audited setup transition), every setup's final view (lifecycle phase,
outcome, re-arm links, features) and the tracking fingerprint. Replay rebuilds them with the same
decision runtime and compares. Recordings made before V1 Pro have no ``setups.json``: replay then
reports "not applicable" (reproducibility only), it never claims a comparison it cannot make.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes.decision.tracking import TRACKING_SCHEMA_VERSION, SetupTracker

SETUPS_SIDECAR = "setups.json"
SETUPS_FORMAT = "hermes-setups"
SETUPS_VERSION = 1


def to_dict(t: SetupTracker, **meta: Any) -> dict:
    return {
        "format": SETUPS_FORMAT, "version": SETUPS_VERSION, "tracking_schema": TRACKING_SCHEMA_VERSION,
        **meta, "counts": t.counts(), "fingerprint": t.fingerprint(),
        "events": [e.row() for e in t.events],
        "setups": [t.by_id[sid].row() for sid in sorted(t.by_id, key=lambda s: (t.by_id[s].created_seq, s))],
    }


def save_setups(t: SetupTracker, path: Path, **meta: Any) -> Path:
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(to_dict(t, **meta), separators=(",", ":")))
    tmp.replace(path)
    return path


def load_setups(path: Path) -> dict:
    d = json.loads(Path(path).read_text())
    if d.get("format") != SETUPS_FORMAT or d.get("version") != SETUPS_VERSION:
        raise ValueError(f"{path}: not a Hermès setups file (format/version)")
    return d


@dataclass(slots=True)
class SetupCompare:
    events: tuple[int, int] = (0, 0)
    events_matched: int = 0
    setups: tuple[int, int] = (0, 0)
    setups_matched: int = 0
    fingerprint_match: bool = False
    first_mismatch: tuple | None = None

    @property
    def equivalent(self) -> bool:
        return (self.events[0] == self.events[1] == self.events_matched
                and self.setups[0] == self.setups[1] == self.setups_matched and self.fingerprint_match)

    def line(self) -> str:
        return (f"setup events {self.events_matched}/{max(self.events)} match  setups "
                f"{self.setups_matched}/{max(self.setups)} match  tracking fingerprint "
                f"{'match' if self.fingerprint_match else 'DIFF'}")


def compare_setups(live: dict, replay: dict) -> SetupCompare:
    r = SetupCompare()
    ea, eb = live.get("events", []), replay.get("events", [])
    sa, sb = live.get("setups", []), replay.get("setups", [])
    r.events = (len(ea), len(eb))
    r.setups = (len(sa), len(sb))
    for x, y in zip(ea, eb):
        if list(x) == list(y):
            r.events_matched += 1
        elif r.first_mismatch is None:
            r.first_mismatch = ("event", x, y)
    for x, y in zip(sa, sb):
        if x == y:
            r.setups_matched += 1
        elif r.first_mismatch is None:
            r.first_mismatch = ("setup", x.get("setup_id"), y.get("setup_id"))
    r.fingerprint_match = live.get("fingerprint") == replay.get("fingerprint")
    return r
