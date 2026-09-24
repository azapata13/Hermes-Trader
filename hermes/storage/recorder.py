"""Asynchronous, non-blocking recorder of the authoritative raw event stream.

Contract (architecture §7, decisions 5–6, C3 amendment E)
---------------------------------------------------------
* ``submit()`` runs on the dispatch thread and NEVER blocks: it appends an object reference to
  a bounded deque (no encoding, no I/O). Encoding and file writes happen on the writer thread.
* One slot is reserved so a gap marker can always be queued after an overflow.
* Overflow: the event is dropped, live processing continues, the dropped seq range accumulates
  into a pending ``RecordingGap`` which is queued ahead of the next accepted event, the
  recording is marked NOT replay-complete from the first dropped seq, telemetry increments.
* Writer failure (e.g. disk full): the lost seq range is recorded as a writer-side gap; the
  writer tries to persist a GAP record and to continue in a new part file. If even that fails,
  the continuity check of ``tools/inspect_recording.py`` still exposes the missing seqs — a
  recording can never be reported replay-complete while raw seqs are missing.
* Gaps are file-level records, not raw events, so the raw ``seq`` space stays contiguous.

Layout: ``<directory>/<YYYY-MM-DD>/<session_id>/part-0001.hrec`` (rotated every
``rotate_minutes``). Every part starts with a self-describing header.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable

from hermes.config import RecorderConfig
from hermes.ibkr.raw_events import RawEvent
from hermes.storage.codec import MAGIC, Encoder

log = logging.getLogger("hermes.recorder")


@dataclass(frozen=True, slots=True)
class RecordingGap:
    first_seq: int
    last_seq: int
    count: int
    reason: str                  # "overflow" | "write_error"
    detected_mono_ns: int
    detected_wall_ns: int


@dataclass(slots=True)
class RecorderStats:
    submitted: int = 0
    written: int = 0
    dropped_overflow: int = 0
    lost_write_error: int = 0
    gaps_queued: int = 0
    gaps_written: int = 0
    write_errors: int = 0
    high_watermark: int = 0
    last_submitted_seq: int = 0
    last_written_seq: int = 0
    replay_complete: bool = True
    first_gap_seq: int | None = None
    parts: int = 0
    bytes_written: int = 0
    current_file: str = ""
    session_dir: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Recorder:
    def __init__(self, cfg: RecorderConfig, meta: dict[str, Any], session_id: str | None = None,
                 wall_ns: Callable[[], int] = time.time_ns, mono_ns: Callable[[], int] = time.perf_counter_ns,
                 open_file: Callable[[Path], BinaryIO] | None = None) -> None:
        self._cfg = cfg
        self._cap = cfg.ring_capacity
        self._meta = dict(meta)
        self._wall = wall_ns
        self._mono = mono_ns
        self._open_file = open_file or (lambda p: open(p, "xb"))
        now = time.gmtime(self._wall() / 1e9)
        self.session_id = session_id or time.strftime("%Y%m%dT%H%M%SZ", now) + f"-{os.getpid()}"
        root = Path(os.path.expanduser(cfg.directory))
        self.session_dir = root / time.strftime("%Y-%m-%d", now) / self.session_id
        self._q: deque[Any] = deque()
        self._pending_gap: list[int] | None = None   # [first_seq, last_seq, count]
        self._closed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fh: BinaryIO | None = None
        self._part_opened_wall = 0
        self._enc = Encoder()
        self.stats = RecorderStats(session_dir=str(self.session_dir))
        self._stats_lock = threading.Lock()      # writer-side fields only (never taken by submit)

    # ================================================================== dispatch thread
    def submit(self, ev: RawEvent) -> bool:
        """Non-blocking. Returns False if the event could not be queued (recording gap)."""
        q = self._q
        st = self.stats
        if self._closed:
            self._drop(ev.seq)
            return False
        n = len(q)
        if self._pending_gap is not None and n < self._cap:
            g = self._pending_gap
            q.append(RecordingGap(g[0], g[1], g[2], "overflow", ev.recv_mono_ns, ev.recv_wall_ns))
            self._pending_gap = None
            st.gaps_queued += 1
            n += 1
        if n >= self._cap - 1:                   # last slot reserved for a gap marker
            self._drop(ev.seq)
            return False
        q.append(ev)
        st.submitted += 1
        st.last_submitted_seq = ev.seq
        if n + 1 > st.high_watermark:
            st.high_watermark = n + 1
        return True

    def _drop(self, seq: int) -> None:
        st = self.stats
        st.dropped_overflow += 1
        st.replay_complete = False
        if st.first_gap_seq is None or seq < st.first_gap_seq:
            st.first_gap_seq = seq
        g = self._pending_gap
        if g is None:
            self._pending_gap = [seq, seq, 1]
        else:
            g[1] = seq
            g[2] += 1

    @property
    def backlog(self) -> int:
        return len(self._q)

    # ================================================================== lifecycle
    def start(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._open_part()
        self._thread = threading.Thread(target=self._run, name="recorder-writer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Drain everything queued, write footer, close. Further submits count as drops."""
        self._closed = True
        if self._pending_gap is not None:
            g = self._pending_gap
            self._q.append(RecordingGap(g[0], g[1], g[2], "overflow", self._mono(), self._wall()))
            self._pending_gap = None
            self.stats.gaps_queued += 1
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.error("recorder writer did not finish within %.1fs; footer not written", timeout)
                return
        self._close_part(final=True)

    # ================================================================== writer thread
    def _run(self) -> None:
        interval = self._cfg.flush_interval_ms / 1000.0
        while True:
            stopping = self._stop.wait(interval)
            self._drain()
            if self._should_rotate():
                self._close_part(final=False)
                self._open_part()
            if stopping and not self._q:
                return

    def _should_rotate(self) -> bool:
        return (self._fh is not None
                and self._wall() - self._part_opened_wall >= self._cfg.rotate_minutes * 60 * 10**9)

    def _drain(self) -> None:
        q = self._q
        batch_max = self._cfg.batch_max
        while q:
            chunk = []
            while q and len(chunk) < batch_max:
                chunk.append(q.popleft())
            self._write_chunk(chunk)

    def _write_chunk(self, chunk: list[Any]) -> None:
        enc = self._enc
        parts: list[bytes] = []
        last_seq = None
        raws = 0
        gaps = 0
        for item in chunk:
            if isinstance(item, RecordingGap):
                parts.append(enc.gap(item.first_seq, item.last_seq, item.count, item.reason,
                                     item.detected_mono_ns, item.detected_wall_ns))
                gaps += 1
            else:
                parts.append(enc.raw(item))
                last_seq = item.seq
                raws += 1
        data = b"".join(parts)
        try:
            if self._fh is None:
                self._open_part()
            assert self._fh is not None
            self._fh.write(data)
            self._fh.flush()
        except Exception as exc:  # noqa: BLE001 - never propagate; record the loss
            self._on_write_error(chunk, exc)
            return
        with self._stats_lock:
            st = self.stats
            st.written += raws
            st.gaps_written += gaps
            st.bytes_written += len(data)
            if last_seq is not None:
                st.last_written_seq = last_seq

    def _on_write_error(self, chunk: list[Any], exc: Exception) -> None:
        seqs = [item.seq for item in chunk if not isinstance(item, RecordingGap)]
        with self._stats_lock:
            st = self.stats
            st.write_errors += 1
            st.lost_write_error += len(seqs)
            st.replay_complete = False
            if seqs and (st.first_gap_seq is None or seqs[0] < st.first_gap_seq):
                st.first_gap_seq = seqs[0]
        log.error("recorder write failed (%s); %d raw events lost", exc, len(seqs))
        # Try to persist the gap in a NEW part file; if that fails too, the continuity check
        # in inspect_recording still reveals the missing seqs.
        self._abandon_part()
        if seqs:
            try:
                self._open_part()
                assert self._fh is not None
                self._fh.write(self._enc.gap(seqs[0], seqs[-1], len(seqs), "write_error", self._mono(), self._wall()))
                self._fh.flush()
                with self._stats_lock:
                    self.stats.gaps_written += 1
            except Exception as exc2:  # noqa: BLE001
                log.error("recorder could not persist gap record: %s", exc2)
                self._abandon_part()

    # ================================================================== files
    def _open_part(self) -> None:
        with self._stats_lock:
            self.stats.parts += 1
            part = self.stats.parts
        path = self.session_dir / f"part-{part:04d}.hrec"
        fh = self._open_file(path)
        self._part_opened_wall = self._wall()
        header = self._enc.header({
            "session_id": self.session_id, "part": part, "created_wall_ns": self._part_opened_wall,
            "seq_origin": 1, "meta": self._meta,
        })
        fh.write(MAGIC + header)
        fh.flush()
        self._fh = fh
        with self._stats_lock:
            self.stats.current_file = str(path)

    def _close_part(self, final: bool) -> None:
        fh = self._fh
        if fh is None:
            return
        try:
            with self._stats_lock:
                info = {"final": final, "last_written_seq": self.stats.last_written_seq,
                        "written": self.stats.written, "replay_complete": self.stats.replay_complete,
                        "first_gap_seq": self.stats.first_gap_seq, "closed_wall_ns": self._wall()}
            fh.write(self._enc.footer(info))
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except (OSError, AttributeError, ValueError):
                pass
        except Exception as exc:  # noqa: BLE001
            log.error("recorder could not write footer: %s", exc)
        finally:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._fh = None

    def _abandon_part(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is not None:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass
