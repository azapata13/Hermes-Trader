"""Codec, recorder (non-blocking, gap semantics), reader/verifier and inspect tool."""

from __future__ import annotations

import dataclasses
import io
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest

from hermes.config import RecorderConfig
from hermes.ibkr import raw_events as R
from hermes.storage.codec import RAW_TYPE_CODES, SCHEMA_VERSION, CodecError, Decoder, Encoder, iter_frames, MAGIC
from hermes.storage.reader import iter_raw_events, list_parts, verify_session
from hermes.storage.recorder import Recorder
from tests.support import RawScript

SAMPLE = {int: 7, float: 21000.25, str: "x", bool: True, Decimal: Decimal("3"), "int | None": 5}


def sample_event(cls, seq=1):
    kw = {}
    for f in dataclasses.fields(cls):
        t = f.type
        if f.name in ("seq",):
            kw[f.name] = seq
        elif t in ("int", "int | None"):
            kw[f.name] = 7
        elif t == "float":
            kw[f.name] = 21000.25
        elif t == "str":
            kw[f.name] = "abc"
        elif t == "bool":
            kw[f.name] = True
        elif t == "Decimal":
            kw[f.name] = Decimal("3.000")
        elif t.startswith("tuple[tuple[float"):
            kw[f.name] = ((0.0, 0.25), (1000.0, 0.5))
        elif t.startswith("tuple[tuple[str"):
            kw[f.name] = (("a", "1"), ("b", "2"))
        else:
            raise AssertionError(f"no sample for {cls.__name__}.{f.name}: {t}")
    return cls(**kw)


def wait_until(pred, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.002)


def cfg(tmp_path: Path, **kw) -> RecorderConfig:
    base = dict(directory=str(tmp_path), ring_capacity=1000, batch_max=100, flush_interval_ms=5, rotate_minutes=60)
    base.update(kw)
    return RecorderConfig(**base)


def raws(n: int, start: int = 1):
    s = RawScript()
    s.seq = start - 1
    return [s.add(R.RawTimerTick, due_mono_ns=i, coalesced=1) for i in range(n)]


# ---------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls", list(RAW_TYPE_CODES), ids=lambda c: c.__name__)
def test_codec_roundtrip_every_raw_type(cls):
    ev = sample_event(cls)
    enc, dec = Encoder(), Decoder()
    data = MAGIC + enc.header({}) + enc.raw(ev)
    recs = [r for _, r in iter_frames(data, len(MAGIC))]
    dec.load_header(recs[0][1])
    out = dec.raw(recs[1])
    assert out == ev and type(out) is cls


def test_every_raw_event_type_is_registered():
    registered = set(RAW_TYPE_CODES)
    concrete = {c for c in vars(R).values() if isinstance(c, type) and dataclasses.is_dataclass(c)
                and c not in (R.RawEvent, R.RawIbkrEvent, R.RawLocalEvent)}
    assert concrete == registered


def test_codec_rejects_wrong_schema():
    dec = Decoder()
    with pytest.raises(CodecError):
        dec.load_header({"format": "hermes-raw", "schema_version": SCHEMA_VERSION + 1, "types": {}})
    with pytest.raises(CodecError):
        dec.load_header({"format": "other", "schema_version": SCHEMA_VERSION, "types": {}})


def test_truncated_frame_detected():
    enc = Encoder()
    data = MAGIC + enc.header({}) + enc.raw(raws(1)[0])
    frames = list(iter_frames(data[:-3], len(MAGIC)))
    assert frames[-1][1] is None


# ---------------------------------------------------------------------------
# Recorder: normal operation
# ---------------------------------------------------------------------------

def test_record_and_verify_complete(tmp_path):
    rec = Recorder(cfg(tmp_path), {"hermes_version": "test"}, session_id="s1")
    rec.start()
    events = raws(500)
    for ev in events:
        assert rec.submit(ev)
    rec.stop()
    rep = verify_session(rec.session_dir)
    assert rep.replay_complete and rep.clean_close and rep.raw_records == 500
    assert (rep.first_seq, rep.last_seq, rep.complete_through_seq) == (1, 500, 500)
    assert list(iter_raw_events(rec.session_dir)) == events
    assert rec.stats.written == 500 and rec.stats.replay_complete


def test_rotation_keeps_continuity(tmp_path):
    clock = {"t": 1_790_000_000 * 10**9}
    rec = Recorder(cfg(tmp_path, rotate_minutes=1), {}, session_id="s2", wall_ns=lambda: clock["t"])
    rec.start()
    for ev in raws(50):
        rec.submit(ev)
    wait_until(lambda: rec.stats.written == 50)
    clock["t"] += 61 * 10**9
    wait_until(lambda: rec.stats.parts == 2)
    for ev in raws(50, start=51):
        rec.submit(ev)
    rec.stop()
    assert len(list_parts(rec.session_dir)) >= 2
    rep = verify_session(rec.session_dir)
    assert rep.replay_complete and rep.raw_records == 100


# ---------------------------------------------------------------------------
# Recorder: never blocks; overflow gap semantics
# ---------------------------------------------------------------------------

class BlockingFile(io.BytesIO):
    def __init__(self, path: Path, gate: threading.Event):
        super().__init__()
        self._real = open(path, "xb")
        self._gate = gate
        self._first = True

    blocked = False

    def write(self, b):
        if not self._first:
            BlockingFile.blocked = True
            self._gate.wait()
        self._first = False
        return self._real.write(b)

    def flush(self):
        self._real.flush()

    def fileno(self):
        return self._real.fileno()

    def close(self):
        self._real.close()


def test_submit_never_blocks_and_overflow_declares_gap(tmp_path):
    gate = threading.Event()
    rec = Recorder(cfg(tmp_path, ring_capacity=16), {}, session_id="s3",
                   open_file=lambda p: BlockingFile(p, gate))
    rec.start()
    events = raws(200)
    BlockingFile.blocked = False
    rec.submit(events[0])
    wait_until(lambda: BlockingFile.blocked)      # writer now blocked inside write()
    t0 = time.perf_counter()
    accepted = [rec.submit(ev) for ev in events[1:150]]
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.05, f"submit blocked: {elapsed:.3f}s for 149 submits"
    assert not all(accepted)
    assert rec.stats.dropped_overflow > 0 and not rec.stats.replay_complete
    first_drop = 1 + accepted.index(False) + 1
    assert rec.stats.first_gap_seq == first_drop
    gate.set()                                    # disk "recovers"
    wait_until(lambda: rec.backlog == 0 and rec.stats.gaps_written >= 1)
    for ev in events[150:160]:
        assert rec.submit(ev)
    rec.stop()
    rep = verify_session(rec.session_dir)
    assert not rep.replay_complete
    assert len(rep.declared_gaps) >= 1 and rep.declared_gaps[0].reason == "overflow"
    assert rep.declared_gaps[0].first_seq == first_drop
    assert rep.undeclared_ranges == []            # every missing seq is covered by a gap record
    assert rep.complete_through_seq == first_drop - 1
    assert rec.stats.gaps_queued >= 1


def test_gap_pending_at_stop_is_still_written(tmp_path):
    gate = threading.Event()
    rec = Recorder(cfg(tmp_path, ring_capacity=16), {}, session_id="s4",
                   open_file=lambda p: BlockingFile(p, gate))
    rec.start()
    for ev in raws(100):
        rec.submit(ev)
    gate.set()
    rec.stop()
    rep = verify_session(rec.session_dir)
    assert rep.declared_gaps and rep.undeclared_ranges == [] and not rep.replay_complete


# ---------------------------------------------------------------------------
# Writer failures (disk full) — amendment E
# ---------------------------------------------------------------------------

class FailingFile:
    """Real file whose writes raise OSError(ENOSPC) while state["fail"] > 0 (decremented per failure)."""

    def __init__(self, path: Path, state: dict):
        if state.get("fail_open"):
            state["fail"] -= 1
            raise OSError(28, "No space left on device")
        self._real = open(path, "xb")
        self._state = state

    def write(self, b):
        if self._state["fail"] > 0:
            self._state["fail"] -= 1
            raise OSError(28, "No space left on device")
        return self._real.write(b)

    def flush(self):
        self._real.flush()

    def fileno(self):
        return self._real.fileno()

    def close(self):
        self._real.close()


def test_write_error_records_gap_in_new_part(tmp_path):
    state = {"fail": 0}
    rec = Recorder(cfg(tmp_path), {}, session_id="s5", open_file=lambda p: FailingFile(p, state))
    rec.start()
    for ev in raws(20):
        rec.submit(ev)
    wait_until(lambda: rec.stats.written == 20)
    state["fail"] = 1                              # next chunk fails; the gap record then fits
    for ev in raws(40, start=21):
        rec.submit(ev)
    wait_until(lambda: rec.stats.write_errors == 1 and rec.stats.gaps_written == 1)
    for ev in raws(10, start=61):
        rec.submit(ev)
    rec.stop()
    rep = verify_session(rec.session_dir)
    assert rec.stats.write_errors >= 1 and not rec.stats.replay_complete
    assert not rep.replay_complete
    assert any(g.reason == "write_error" for g in rep.declared_gaps)
    assert rep.complete_through_seq == 20


def test_missing_seqs_without_gap_record_are_never_complete(tmp_path):
    """Disk full so badly that even the gap record cannot be written: continuity still catches it."""
    state = {"fail": 0}
    rec = Recorder(cfg(tmp_path), {}, session_id="s6", open_file=lambda p: FailingFile(p, state))
    rec.start()
    for ev in raws(20):
        rec.submit(ev)
    wait_until(lambda: rec.stats.written == 20)
    state["fail"] = 2                              # data chunk AND the new part (gap record) fail
    for ev in raws(30, start=21):
        rec.submit(ev)
    wait_until(lambda: state["fail"] == 0 and rec.stats.write_errors == 1)
    time.sleep(0.02)                               # space comes back
    for ev in raws(10, start=51):
        rec.submit(ev)
    rec.stop()
    rep = verify_session(rec.session_dir)
    assert not rep.replay_complete
    assert rep.undeclared_ranges and rep.undeclared_ranges[0][0] == 21
    assert rep.complete_through_seq == 20


def test_unclean_end_and_truncated_tail(tmp_path):
    rec = Recorder(cfg(tmp_path), {}, session_id="s7")
    rec.start()
    for ev in raws(30):
        rec.submit(ev)
    wait_until(lambda: rec.stats.written == 30)
    rec._stop.set()           # simulate crash: writer exits without footer
    rec._thread.join()
    rec._abandon_part()
    part = list_parts(rec.session_dir)[0]
    data = part.read_bytes()
    part.write_bytes(data[:-5])
    rep = verify_session(rec.session_dir)
    assert not rep.replay_complete and not rep.clean_close
    assert rep.parts[0].truncated_at is not None
    assert rep.complete_through_seq == 29


def test_inspect_tool(tmp_path, capsys):
    from tools.inspect_recording import main
    rec = Recorder(cfg(tmp_path), {"hermes_version": "0.4.0"}, session_id="s8")
    rec.start()
    for ev in raws(10):
        rec.submit(ev)
    rec.stop()
    assert main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "REPLAY-COMPLETE: YES" in out and "RawTimerTick" in out
    assert main([str(rec.session_dir), "--json"]) == 0
    assert main([str(tmp_path / "nothing")]) == 2
