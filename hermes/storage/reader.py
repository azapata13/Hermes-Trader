"""Recording reader and continuity verifier.

``verify_session`` is the authority on replay completeness (C3 amendment E): it recomputes
raw ``seq`` continuity from the records actually present on disk. A recording is reported
replay-complete ONLY if every seq from ``seq_origin`` to the last seq is present, no gap
record exists, nothing is truncated/corrupt, and the final part was closed cleanly. Missing
seqs without a gap record (e.g. the gap record itself could not be written because the disk
was full) are reported as UNDECLARED gaps and also make the recording incomplete.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from hermes.ibkr.raw_events import RawEvent
from hermes.storage.codec import KIND_FOOTER, KIND_GAP, KIND_HEADER, KIND_RAW, MAGIC, CodecError, Decoder, iter_frames


@dataclass(slots=True)
class GapRecord:
    first_seq: int
    last_seq: int
    count: int
    reason: str
    part: str


@dataclass(slots=True)
class PartReport:
    path: str
    header: dict | None = None
    raw_records: int = 0
    first_seq: int | None = None
    last_seq: int | None = None
    footer: dict | None = None
    truncated_at: int | None = None
    error: str | None = None


@dataclass(slots=True)
class SessionReport:
    session_dir: str
    parts: list[PartReport] = field(default_factory=list)
    counts_by_type: Counter = field(default_factory=Counter)
    raw_records: int = 0
    first_seq: int | None = None
    last_seq: int | None = None
    declared_gaps: list[GapRecord] = field(default_factory=list)
    missing_ranges: list[tuple[int, int]] = field(default_factory=list)
    undeclared_ranges: list[tuple[int, int]] = field(default_factory=list)
    order_errors: int = 0
    clean_close: bool = False
    complete_through_seq: int = 0
    replay_complete: bool = False
    problems: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def list_parts(session_dir: Path) -> list[Path]:
    return sorted(session_dir.glob("part-*.hrec"))


def iter_part(path: Path, report: PartReport | None = None) -> Iterator[tuple[int, object]]:
    """Yield (kind, payload) where payload is a RawEvent, GapRecord or dict."""
    data = path.read_bytes()
    rep = report or PartReport(str(path))
    if not data.startswith(MAGIC):
        rep.error = "bad magic (not a Hermès recording)"
        return
    dec = Decoder()
    have_header = False
    for offset, rec in iter_frames(data, len(MAGIC)):
        if rec is None:
            rep.truncated_at = offset
            return
        kind = rec[0]
        try:
            if kind == KIND_HEADER:
                dec.load_header(rec[1])
                rep.header = rec[1]
                have_header = True
                yield kind, rec[1]
            elif not have_header:
                rep.error = "record before header"
                return
            elif kind == KIND_RAW:
                ev = dec.raw(rec)
                rep.raw_records += 1
                if rep.first_seq is None:
                    rep.first_seq = ev.seq
                rep.last_seq = ev.seq
                yield kind, ev
            elif kind == KIND_GAP:
                yield kind, GapRecord(rec[1], rec[2], rec[3], rec[4], path.name)
            elif kind == KIND_FOOTER:
                rep.footer = rec[1]
                yield kind, rec[1]
            else:
                rep.error = f"unknown record kind {kind}"
                return
        except CodecError as exc:
            rep.error = str(exc)
            return


def iter_raw_events(session_dir: Path) -> Iterator[RawEvent]:
    """All raw events of a session in recorded order (for replay / tests)."""
    for part in list_parts(session_dir):
        for kind, payload in iter_part(part):
            if kind == KIND_RAW:
                yield payload  # type: ignore[misc]


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


def verify_session(session_dir: Path) -> SessionReport:
    session_dir = Path(session_dir)
    rep = SessionReport(str(session_dir))
    parts = list_parts(session_dir)
    if not parts:
        rep.problems.append("no part files found")
        return rep
    expected: int | None = None
    origin = 1
    for i, path in enumerate(parts):
        pr = PartReport(str(path))
        rep.parts.append(pr)
        for kind, payload in iter_part(path, pr):
            if kind == KIND_HEADER:
                if i == 0:
                    origin = int(payload.get("seq_origin", 1))  # type: ignore[union-attr]
                    rep.meta = payload.get("meta", {})  # type: ignore[union-attr]
                    expected = origin
            elif kind == KIND_RAW:
                ev = payload
                rep.raw_records += 1
                rep.counts_by_type[type(ev).__name__] += 1
                seq = ev.seq  # type: ignore[attr-defined]
                if rep.first_seq is None:
                    rep.first_seq = seq
                if expected is None:
                    expected = origin
                if seq > expected:
                    rep.missing_ranges.append((expected, seq - 1))
                elif seq < expected:
                    rep.order_errors += 1
                expected = max(expected, seq + 1)
                rep.last_seq = seq if rep.last_seq is None else max(rep.last_seq, seq)
            elif kind == KIND_GAP:
                rep.declared_gaps.append(payload)  # type: ignore[arg-type]
        if pr.error:
            rep.problems.append(f"{path.name}: {pr.error}")
        if pr.truncated_at is not None:
            rep.problems.append(f"{path.name}: truncated/corrupt tail at byte {pr.truncated_at}")
    last = rep.parts[-1]
    rep.clean_close = bool(last.footer and last.footer.get("final")) and last.truncated_at is None
    if not rep.clean_close:
        rep.problems.append("final part not closed cleanly (no final footer) — tail may be missing")
    for pr in rep.parts[:-1]:
        if pr.footer is None:
            rep.problems.append(f"{Path(pr.path).name}: no footer (writer failure or crash)")
    rep.undeclared_ranges = _subtract(rep.missing_ranges, [(g.first_seq, g.last_seq) for g in rep.declared_gaps])
    if rep.declared_gaps:
        rep.problems.append(f"{len(rep.declared_gaps)} declared recording gap(s)")
    if rep.undeclared_ranges:
        rep.problems.append(f"{len(rep.undeclared_ranges)} UNDECLARED missing seq range(s)")
    if rep.order_errors:
        rep.problems.append(f"{rep.order_errors} out-of-order/duplicate seq(s)")
    firsts = [lo for lo, _ in rep.missing_ranges] + [g.first_seq for g in rep.declared_gaps]
    if firsts:
        rep.complete_through_seq = min(firsts) - 1
    elif rep.last_seq is not None:
        rep.complete_through_seq = rep.last_seq
    rep.replay_complete = (
        rep.raw_records > 0 and not rep.missing_ranges and not rep.declared_gaps and not rep.order_errors
        and rep.clean_close and not any(p.error or p.truncated_at is not None for p in rep.parts)
    )
    return rep
