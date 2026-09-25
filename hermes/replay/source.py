"""Recording source for replay (C6): reads the authoritative raw ``.hrec`` parts WITHOUT TWS.

* Validates explicitly: file magic, ``format``, codec ``schema_version``, recorded raw-type field
  sets (by name; any mismatch = different schema, never guessed), ``normalizer_version``, and
  per-part header consistency. Unsupported versions raise ``ReplayIncompatible``. Migrations can
  later be added at these two boundaries (codec schema, normalizer version).
* Tracks integrity while streaming (single pass): declared ``RecordingGap`` records, missing
  raw seqs, undeclared discontinuities, out-of-order seqs, corrupt frames/parts, a truncated final
  record, clean close (final footer), contract metadata presence, and a SHA-256 digest of the raw
  record bytes (raw-event reproducibility).
* A truncated tail keeps every complete record before it; the recording is then "complete up to
  its last complete seq" but never labelled cleanly closed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from hermes.ibkr import raw_events as R
from hermes.ibkr.normalizer import NORMALIZER_VERSION
from hermes.storage.codec import (
    FORMAT_NAME,
    KIND_FOOTER,
    KIND_GAP,
    KIND_HEADER,
    KIND_RAW,
    MAGIC,
    SCHEMA_VERSION,
    TAIL_TRUNCATED,
    CodecError,
    Decoder,
    scan_frames,
)

SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION})
SUPPORTED_NORMALIZER_VERSIONS = frozenset({NORMALIZER_VERSION})


class ReplayIncompatible(Exception):
    """The recording cannot be interpreted by this code (unknown schema / normalizer / format)."""


@dataclass(slots=True)
class RecordingInfo:
    session_dir: str
    session_id: str
    parts: list[str]
    schema_version: int
    normalizer_version: int | None
    meta: dict

    @property
    def config(self) -> dict | None:
        c = self.meta.get("config")
        return c if isinstance(c, dict) else None


@dataclass(slots=True)
class Integrity:
    seq_origin: int = 1
    raw_records: int = 0
    first_seq: int | None = None
    last_seq: int | None = None
    declared_gaps: list[tuple[int, int, int, str]] = field(default_factory=list)
    missing_ranges: list[tuple[int, int]] = field(default_factory=list)
    undeclared_ranges: list[tuple[int, int]] = field(default_factory=list)
    order_errors: int = 0
    corrupt: list[str] = field(default_factory=list)
    truncated_tail: bool = False
    clean_close: bool = False
    parts_without_footer: list[str] = field(default_factory=list)
    contract_details: bool = False
    market_rule: bool = False
    first_break_seq: int | None = None      # first seq that is NOT covered by a contiguous prefix
    stopped_at_seq: int | None = None       # stop_at_gap: replay stopped before this seq
    raw_digest: str = ""

    @property
    def contiguous(self) -> bool:
        """No raw data missing/corrupt before the last complete record (a truncated tail is allowed)."""
        return (self.raw_records > 0 and not self.declared_gaps and not self.missing_ranges
                and not self.order_errors and not self.corrupt)

    @property
    def replay_complete(self) -> bool:
        return self.contiguous and self.clean_close and not self.truncated_tail

    @property
    def complete_through_seq(self) -> int:
        if self.first_break_seq is not None:
            return self.first_break_seq - 1
        return self.last_seq or 0

    @property
    def label(self) -> str:
        if self.replay_complete:
            return "COMPLETE (clean close, contiguous)"
        if self.contiguous:
            why = "truncated final record" if self.truncated_tail else "no final footer"
            return f"COMPLETE UP TO seq {self.last_seq} — NOT CLEANLY CLOSED ({why})"
        return f"INCOMPLETE — best-effort diagnostic only; deterministic through seq {self.complete_through_seq}"

    def problems(self) -> list[str]:
        out = []
        for a, b, n, why in self.declared_gaps:
            out.append(f"declared gap seq {a}-{b} ({n} events, {why})")
        for a, b in self.undeclared_ranges:
            out.append(f"UNDECLARED missing seq {a}-{b}")
        if self.order_errors:
            out.append(f"{self.order_errors} out-of-order/duplicate seq(s)")
        out.extend(self.corrupt)
        if self.truncated_tail:
            out.append("truncated final record (writer crash); complete records before it are kept")
        if not self.clean_close:
            out.append("final part not closed cleanly (no final footer)")
        for p in self.parts_without_footer:
            out.append(f"{p}: no footer")
        if not self.contract_details:
            out.append("no contract details recorded (no PriceGrid: market data cannot be normalized)")
        elif not self.market_rule:
            out.append("no market rule recorded (PriceGrid may be unavailable)")
        return out


def resolve_session(path: Path) -> Path:
    path = Path(path).expanduser()
    if path.is_file() and path.suffix == ".hrec":
        return path.parent
    return path


class RecordingSource:
    """Streams raw events of one recorded session. Never touches IBKR, sockets or clocks."""

    def __init__(self, session_dir: Path) -> None:
        self.session_dir = resolve_session(session_dir)
        self.parts = sorted(self.session_dir.glob("part-*.hrec"))
        if not self.parts:
            raise ReplayIncompatible(f"no part-*.hrec files in {self.session_dir}")
        self.info = self._read_info(self.parts[0])
        self.integrity = Integrity()
        self._consumed = False

    # ------------------------------------------------------------------ header validation
    @staticmethod
    def _validate_header(body: object, where: str) -> dict:
        if not isinstance(body, dict):
            raise ReplayIncompatible(f"{where}: malformed header")
        if body.get("format") != FORMAT_NAME:
            raise ReplayIncompatible(f"{where}: not a Hermès raw recording (format={body.get('format')!r})")
        sv = body.get("schema_version")
        if sv not in SUPPORTED_SCHEMA_VERSIONS:
            raise ReplayIncompatible(f"{where}: unsupported recording schema_version {sv!r} "
                                     f"(supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}); a migration is required")
        meta = body.get("meta") or {}
        nv = meta.get("normalizer_version")
        if nv is not None and nv not in SUPPORTED_NORMALIZER_VERSIONS:
            raise ReplayIncompatible(f"{where}: recorded normalizer_version {nv!r} not supported "
                                     f"(current {NORMALIZER_VERSION}); a migration is required")
        try:
            Decoder().load_header(body)                  # raw-type table by name, field sets must match
        except CodecError as exc:
            raise ReplayIncompatible(f"{where}: {exc}") from exc
        return body

    def _read_info(self, first: Path) -> RecordingInfo:
        data = first.read_bytes()
        if not data.startswith(MAGIC):
            raise ReplayIncompatible(f"{first.name}: bad magic (not a Hermès recording)")
        for _off, rec, _payload in scan_frames(data, len(MAGIC)):
            if rec is None or rec[0] != KIND_HEADER or len(rec) < 2:
                raise ReplayIncompatible(f"{first.name}: first record is not a readable header")
            body = self._validate_header(rec[1], first.name)
            meta = body.get("meta") or {}
            return RecordingInfo(str(self.session_dir), str(body.get("session_id", self.session_dir.name)),
                                 [p.name for p in self.parts], body["schema_version"],
                                 meta.get("normalizer_version"), meta)
        raise ReplayIncompatible(f"{first.name}: empty recording")

    # ------------------------------------------------------------------ streaming
    def events(self, stop_at_gap: bool = False) -> Iterator[R.RawEvent]:
        """Yield raw events in recorded order while updating ``self.integrity``.

        ``stop_at_gap``: stop before the first event that follows missing raw data, so only the
        deterministic prefix is replayed.
        """
        if self._consumed:
            raise RuntimeError("RecordingSource is single-pass; open a new one")
        self._consumed = True
        ig = self.integrity
        h = hashlib.sha256()
        expected: int | None = None
        stop = False
        for i, path in enumerate(self.parts):
            last_part = i == len(self.parts) - 1
            data = path.read_bytes()
            if not data.startswith(MAGIC):
                ig.corrupt.append(f"{path.name}: bad magic")
                self._break(expected or ig.seq_origin)
                continue
            dec = Decoder()
            header_seen = False
            footer = None
            for off, rec, payload in scan_frames(data, len(MAGIC)):
                if rec is None:
                    if payload == TAIL_TRUNCATED and last_part:
                        ig.truncated_tail = True
                    else:
                        ig.corrupt.append(f"{path.name}: {payload} at byte {off}")
                        self._break(expected or ig.seq_origin)
                    break
                kind = rec[0]
                try:
                    if kind == KIND_HEADER:
                        body = self._validate_header(rec[1], path.name)
                        if body.get("session_id") != self.info.session_id and i > 0:
                            raise ReplayIncompatible(f"{path.name}: belongs to another session")
                        dec.load_header(body)
                        if i == 0:
                            ig.seq_origin = int(body.get("seq_origin", 1))
                            expected = ig.seq_origin
                        header_seen = True
                    elif not header_seen:
                        ig.corrupt.append(f"{path.name}: record before header")
                        self._break(expected or ig.seq_origin)
                        break
                    elif kind == KIND_RAW:
                        ev = dec.raw(rec)
                        seq = ev.seq
                        if expected is None:
                            expected = ig.seq_origin
                        if seq > expected:
                            ig.missing_ranges.append((expected, seq - 1))
                            self._break(expected)
                        elif seq < expected:
                            ig.order_errors += 1
                            self._break(seq)
                        if stop_at_gap and ig.first_break_seq is not None:
                            ig.stopped_at_seq = seq
                            stop = True
                            break
                        expected = max(expected, seq + 1)
                        ig.raw_records += 1
                        if ig.first_seq is None:
                            ig.first_seq = seq
                        ig.last_seq = seq if ig.last_seq is None else max(ig.last_seq, seq)
                        h.update(payload)  # type: ignore[arg-type]
                        t = type(ev)
                        if t is R.RawContractDetails:
                            ig.contract_details = True
                        elif t is R.RawMarketRule:
                            ig.market_rule = True
                        yield ev
                    elif kind == KIND_GAP:
                        a, b, n, why = rec[1], rec[2], rec[3], rec[4]
                        ig.declared_gaps.append((a, b, n, why))
                        self._break(a)
                        if stop_at_gap:
                            ig.stopped_at_seq = a
                            stop = True
                            break
                    elif kind == KIND_FOOTER:
                        footer = rec[1]
                    else:
                        ig.corrupt.append(f"{path.name}: unknown record kind {kind} at byte {off}")
                        self._break(expected or ig.seq_origin)
                        break
                except (CodecError, TypeError) as exc:
                    ig.corrupt.append(f"{path.name}: undecodable record at byte {off}: {exc}")
                    self._break(expected or ig.seq_origin)
                    break
            if footer is None and not last_part:
                ig.parts_without_footer.append(path.name)
            if last_part:
                ig.clean_close = bool(isinstance(footer, dict) and footer.get("final")) and not ig.truncated_tail
            if stop:
                break
        gaps = [(a, b) for a, b, _, _ in ig.declared_gaps]
        ig.undeclared_ranges = [r for r in _subtract(ig.missing_ranges, gaps)]
        ig.raw_digest = h.hexdigest()

    def _break(self, seq: int) -> None:
        ig = self.integrity
        if ig.first_break_seq is None or seq < ig.first_break_seq:
            ig.first_break_seq = seq


def _subtract(ranges: list[tuple[int, int]], covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for lo, hi in ranges:
        segs = [(lo, hi)]
        for clo, chi in covered:
            nxt = []
            for a, b in segs:
                if chi < a or clo > b:
                    nxt.append((a, b))
                    continue
                if a < clo:
                    nxt.append((a, clo - 1))
                if chi < b:
                    nxt.append((chi + 1, b))
            segs = nxt
        out.extend(segs)
    return out
