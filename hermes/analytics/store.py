"""Append-only store of finished setups (``~/hermes-data/setups/setups.jsonl``): the recorded data
behind every historical statistic. One JSON line per finished setup (``SetupView.row()``).

Writes happen on a dedicated thread (never on the market dispatch thread). Loading is tolerant:
malformed lines, unknown schema versions and duplicate setup_ids are skipped and COUNTED, never
guessed. Older rows without newer optional fields stay readable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from threading import Thread

log = logging.getLogger("hermes.analytics.store")

DEFAULT_STORE = "~/hermes-data/setups/setups.jsonl"
SUPPORTED_SCHEMAS = frozenset({1})
_STOP = object()


@dataclass(slots=True)
class LoadResult:
    rows: list[dict] = field(default_factory=list)
    malformed: int = 0
    unsupported_schema: int = 0
    duplicates: int = 0
    unfinished: int = 0


def load_rows(path: str | Path) -> LoadResult:
    p = Path(path).expanduser()
    out = LoadResult()
    if not p.exists():
        return out
    seen: set[str] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            out.malformed += 1
            continue
        if not isinstance(row, dict) or not isinstance(row.get("outcome"), dict) or not row.get("setup_id"):
            out.malformed += 1
            continue
        if row.get("schema") not in SUPPORTED_SCHEMAS:
            out.unsupported_schema += 1
            continue
        if row["outcome"].get("status") == "OPEN":
            out.unfinished += 1
            continue
        if row["setup_id"] in seen:
            out.duplicates += 1
            continue
        seen.add(row["setup_id"])
        out.rows.append(row)
    return out


class SetupStore:
    """In-memory finished rows + asynchronous JSONL append."""

    def __init__(self, path: str | Path | None = DEFAULT_STORE, max_rows: int = 200_000) -> None:
        self.path = Path(path).expanduser() if path else None
        self.max_rows = max_rows
        loaded = load_rows(self.path) if self.path else LoadResult()
        self.load_result = loaded
        self.rows: list[dict] = loaded.rows[-max_rows:]
        self._ids = {r["setup_id"] for r in self.rows}
        self._q: Queue[object] = Queue()
        self._writer: Thread | None = None
        self.write_errors = 0

    def start(self) -> None:
        if self.path is None or self._writer is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = Thread(target=self._run, name="hermes-setup-store", daemon=True)
        self._writer.start()

    def add(self, row: dict) -> bool:
        """Record one FINISHED setup (idempotent per setup_id). Called from the dispatch thread: no I/O."""
        if row.get("outcome", {}).get("status") == "OPEN" or row.get("setup_id") in self._ids:
            return False
        self._ids.add(row["setup_id"])
        self.rows.append(row)
        if len(self.rows) > self.max_rows:
            self.rows = self.rows[-self.max_rows:]
        if self._writer is not None:
            self._q.put(row)
        return True

    def close(self, timeout: float = 3.0) -> None:
        if self._writer is None:
            return
        self._q.put(_STOP)
        self._writer.join(timeout)
        self._writer = None

    def _run(self) -> None:
        assert self.path is not None
        while True:
            item = self._q.get()
            if item is _STOP:
                return
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(item, separators=(",", ":"), sort_keys=True) + "\n")
            except OSError as exc:
                self.write_errors += 1
                log.error("could not append to the setup store: %s", exc)
