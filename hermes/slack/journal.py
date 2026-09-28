"""Append-only human approval audit journal for D1.

File I/O is performed by a dedicated writer thread.  The market-data dispatch
thread only appends an immutable entry to memory and enqueues one JSON line.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
from pathlib import Path
from queue import Queue
from threading import Lock, Thread
from typing import Any

log = logging.getLogger("hermes.slack.journal")

_STOP = object()


@dataclass(frozen=True, slots=True)
class ApprovalAuditEntry:
    interaction_id: str
    slack_user_id: str
    action: str
    interaction_wall_ns: int | None
    processed_wall_ns: int
    processed_seq: int
    setup_id: str
    proposal_id: str
    clicked_approval_view_id: str
    displayed_approval_view_id: str | None
    current_approval_view_id: str | None
    current_status: str | None
    current_approval_allowed_now: bool
    human_response: dict[str, Any] | None
    result_codes: tuple[str, ...]
    accepted_human_intent: bool
    authorizes_execution: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["result_codes"] = list(self.result_codes)
        return d


class ApprovalJournal:
    """In-memory audit list plus optional asynchronous JSONL persistence."""

    def __init__(self, path: str | Path | None = None, max_entries: int = 100_000) -> None:
        self.path = Path(path).expanduser() if path else None
        self.max_entries = max_entries
        self._entries: list[ApprovalAuditEntry] = []
        self._lock = Lock()
        self._q: Queue[object] = Queue()
        self._writer: Thread | None = None
        self.write_errors = 0
        self.dropped = 0

    @property
    def entries(self) -> tuple[ApprovalAuditEntry, ...]:
        with self._lock:
            return tuple(self._entries)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def start(self) -> None:
        if self.path is None or self._writer is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = Thread(target=self._run, name="hermes-approval-journal", daemon=True)
        self._writer.start()

    def append(self, entry: ApprovalAuditEntry) -> None:
        with self._lock:
            if len(self._entries) >= self.max_entries:
                self.dropped += 1
            else:
                self._entries.append(entry)
        if self.path is not None:
            self._q.put(json.dumps(entry.to_dict(), sort_keys=True, separators=(",", ":")))

    def close(self, timeout: float = 2.0) -> None:
        if self._writer is None:
            return
        self._q.put(_STOP)
        self._writer.join(timeout)
        if self._writer.is_alive():
            log.warning("approval journal writer did not stop within %.1fs", timeout)
        self._writer = None

    def _run(self) -> None:
        assert self.path is not None
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                while True:
                    item = self._q.get()
                    try:
                        if item is _STOP:
                            return
                        fh.write(str(item) + "\n")
                        fh.flush()
                    except OSError as exc:
                        self.write_errors += 1
                        log.error("human approval journal write failed: %s", exc)
                    finally:
                        self._q.task_done()
        except OSError as exc:
            self.write_errors += 1
            failed_path = self.path
            self.path = None
            log.error("could not open human approval journal %s: %s", failed_path, exc)
