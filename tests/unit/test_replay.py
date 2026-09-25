"""C6 deterministic replay: same Normalizer + MarketEngine as live, fed from raw .hrec recordings.

Every scenario is (1) processed directly (the live-equivalent Harness), (2) written as a raw
recording and replayed; the replayed engine must equal the direct one (snapshot + state hash), and
two replays must produce identical checkpoint sequences. FAST mode only; PACED uses a fake sleeper.
"""

from __future__ import annotations

import dataclasses
import random
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes.config import BarsConfig, HermesConfig
from hermes.market.bars import BarFlag
from hermes.market.classify import Aggressor, ClassMethod
from hermes.market.events import ConnectionState
from hermes.market.health import ConflictPhase
from hermes.market.orderbook import BookState
from hermes.replay import fingerprint as fp
from hermes.replay.checkpoints import CheckpointPolicy, Checkpointer, compare_checkpoints, load_checkpoints
from hermes.replay.clock import ReplayClock
from hermes.replay.runner import ReplayMode, ReplayOptions, replay_session
from hermes.replay.source import RecordingSource, ReplayIncompatible
from hermes.storage.codec import MAGIC
from tests.support import BASE, BBO, DEPTH, TICK, TRADES, Harness, RawScript, write_hrec

S = 1_000_000_000
T0 = 1790085600                                   # 2026-09-22 14:00 UTC (09:00 CDT, RTH)
WEEK = dict(trading_hours="20260921:1700-20260922:1600;20260922:1700-20260923:1600",
            liquid_hours="20260922:0830-20260922:1500", time_zone_id="US/Central")
POLICY = CheckpointPolicy(every_n=25)


def ticks(sc: RawScript, ms: float, step: float = 250) -> RawScript:
    t = 0.0
    while t < ms:
        sc.advance(step)
        sc.tick()
        t += step
    return sc


def ready(**contract) -> RawScript:
    sc = RawScript(wall0=T0 * S)
    sc.bootstrap(**(contract or WEEK))
    sc.seed_book()
    return ticks(sc, 600)


def replay(tmp_path: Path, sc: RawScript, name="s", **opt):
    d = write_hrec(tmp_path / name, sc.events)
    opt.setdefault("policy", POLICY)
    return replay_session(d, ReplayOptions(**opt))


def check(tmp_path: Path, sc: RawScript):
    """Replay == direct processing, and replay is repeatable. Returns (result, harness)."""
    h = Harness().run(sc)
    r1 = replay(tmp_path, sc, "a")
    r2 = replay(tmp_path, sc, "b")
    assert r1.integrity.replay_complete and r1.internal_errors == 0
    assert r1.engine.snapshot() == h.engine.snapshot()
    assert r1.final_hash == fp.state_hash(h.engine)
    assert [(c.key(), c.hash) for c in r1.checkpoints] == [(c.key(), c.hash) for c in r2.checkpoints]
    assert r1.final_hash == r2.final_hash and r1.integrity.raw_digest == r2.integrity.raw_digest
    return r1, h


def inst(r):
    return r.engine.instruments[1]


# ============================================================================ BASIC

def test_known_sequence_replays_to_expected_state(tmp_path):
    sc = ready()
    sc.trade(TRADES, BASE + TICK, 2)
    r, _ = check(tmp_path, sc)
    snap = r.engine.snapshot().instrument(1)
    assert snap.market_data_ok and snap.book.state is BookState.VALID
    assert r.raw_events == len(sc.events) and r.first_seq == 1 and r.final_seq == len(sc.events)
    assert r.classified_trades == 1 and r.buy_volume == 2 and r.config_source == "recorded"
    assert r.session_available and r.book_state == "valid" and r.checkpoint_count > 0


def test_fast_twice_and_fast_vs_paced_identical(tmp_path):
    sc = ready()
    for i in range(40):
        sc.advance(137)
        sc.trade(TRADES, BASE + TICK * (i % 3), 1 + i % 4)
        if i % 5 == 0:
            sc.tick()
    d = write_hrec(tmp_path / "s", sc.events)
    fast1 = replay_session(d, ReplayOptions(policy=POLICY))
    fast2 = replay_session(d, ReplayOptions(policy=POLICY))
    clock = {"t": 0}
    slept = []

    def fake_timer():
        return clock["t"]

    def fake_sleep(s):                           # no real sleeping: advance a fake clock
        slept.append(s)
        clock["t"] += int(s * 1e9)
    paced = replay_session(d, ReplayOptions(mode=ReplayMode.PACED, speed=2.0, policy=POLICY,
                                            timer=fake_timer, sleeper=fake_sleep))
    seqs = [[(c.key(), c.hash) for c in x.checkpoints] for x in (fast1, fast2, paced)]
    assert seqs[0] == seqs[1] == seqs[2] and fast1.final_hash == fast2.final_hash == paced.final_hash
    span = (sc.events[-1].recv_mono_ns - sc.events[0].recv_mono_ns) / 1e9
    assert sum(slept) == pytest.approx(span / 2.0, rel=1e-6)     # recorded timing / speed
    half = replay_session(d, ReplayOptions(mode=ReplayMode.PACED, speed=0.5, policy=POLICY, timer=fake_timer,
                                           sleeper=fake_sleep, max_sleep_s=0.01))
    assert half.final_hash == fast1.final_hash


# ============================================================================ ORDER BOOK

def test_book_insert_update_delete(tmp_path):
    sc = ready()
    sc.depth(DEPTH, 0, 1, 1, BASE, 99)               # update best bid size
    sc.depth(DEPTH, 4, 2, 0, BASE + 5 * TICK, 0)     # delete worst ask
    sc.depth(DEPTH, 4, 0, 0, BASE + 5 * TICK, 7)     # insert it back with a new size
    ticks(sc, 300)
    r, _ = check(tmp_path, sc)
    book = r.engine.snapshot().instrument(1).book
    assert book.bids[0] == (84000, 99) and book.asks[4] == (84005, 7)


def test_real_tws_repeated_terminal_delete_does_not_force_resync(tmp_path):
    """Replay regression for DELETE ASK pos=9 twice on a 10-row CME window."""
    sc = RawScript(wall0=T0 * S)
    sc.bootstrap(**WEEK)
    sc.seed_book(rows=10)
    ticks(sc, 600)

    sc.depth(DEPTH, 9, 2, 0, 0.0, 0)
    sc.depth(DEPTH, 9, 2, 0, 0.0, 0)
    ticks(sc, 300)

    r, _ = check(tmp_path, sc)

    book = r.engine.snapshot().instrument(1).book
    assert book.state is BookState.VALID
    assert len(book.asks) == 9

    live_book = r.engine.instruments[1].book
    assert live_book is not None
    assert live_book.counters.opaque_tail_deletes == 1
    assert live_book.counters.violations == {}



def test_317_reset_rebuild_and_old_generation_callbacks_ignored(tmp_path):
    sc = ready()
    sc.error(DEPTH, 317, "Market depth data has been RESET")
    sc.seed_book(l1=0)
    ticks(sc, 600)
    sc.request("cancelMktDepth", DEPTH)
    sc.request("reqMktDepth", 20_001, num_rows=10)
    sc.seed_book(depth=20_001, l1=0)
    for _ in range(3):
        sc.depth(DEPTH, 0, 1, 1, BASE - 50, 999)        # late callbacks of the OLD generation
    ticks(sc, 600)
    r, _ = check(tmp_path, sc)
    e = r.engine
    b = e.snapshot().instrument(1).book
    assert b.state is BookState.VALID and b.epoch >= 2 and (84000 - 200, 999) not in b.bids
    assert e.counters.inactive_callbacks == 3 and e.counters.errors_by_code[317] == 1


# ============================================================================ CLASSIFIER

def test_classifier_buy_sell_unknown_history_tick_rule_and_resets(tmp_path):
    sc = ready()
    sc.trade(TRADES, BASE + TICK, 1)                  # at ask -> BUY
    sc.trade(TRADES, BASE, 2)                         # at bid -> SELL
    sc.advance(300)
    sc.bbo(BBO, BASE, BASE + TICK)                    # fresh quote
    sc.advance(5)
    sc.bbo(BBO, BASE + TICK, BASE + 2 * TICK)         # quote moves up before the trade callback
    sc.advance(10)
    sc.trade(TRADES, BASE + TICK, 3)                  # old ask lifted -> HISTORICAL_QUOTE BUY
    sc.advance(200)
    sc.bbo(BBO, BASE - TICK, BASE + 3 * TICK)         # wide spread
    sc.advance(200)
    sc.trade(TRADES, BASE, 1)                         # inside -> TICK_RULE
    sc.trade(TRADES, BASE, 1, special="Z")            # ineligible -> UNKNOWN
    sc.request("reqTickByTickData", 20_002, tick_type="BidAsk")   # BBO resubscription resets quotes
    sc.trade(TRADES, BASE, 1)
    r, h = check(tmp_path, sc)
    t = inst(r).tape.trades()
    assert [x.aggressor for x in t[:5]] == [Aggressor.BUY, Aggressor.SELL, Aggressor.BUY, Aggressor.SELL,
                                            Aggressor.UNKNOWN]
    assert [x.method for x in t[:4]] == [ClassMethod.DIRECT_QUOTE, ClassMethod.DIRECT_QUOTE,
                                         ClassMethod.HISTORICAL_QUOTE, ClassMethod.TICK_RULE]
    assert t[-1].aggressor is Aggressor.UNKNOWN and inst(r).classifier.epoch == inst(h).classifier.epoch > 0
    assert inst(r).classifier.tick_reference == inst(h).classifier.tick_reference


# ============================================================================ BARS

def test_bars_empty_late_flags_session_vwap_identical(tmp_path):
    sc = ready()
    prices = [BASE, BASE + TICK, BASE + 2 * TICK, BASE + TICK]
    for i in range(24):                               # ~2 minutes of prints
        sc.at(T0 + 1 + i * 5)
        sc.trade(TRADES, prices[i % 4], 1 + i % 3)
        sc.tick()
    sc.at(T0 + 125.6)
    sc.tick()
    sc.at(T0 + 126)
    sc.tick()
    sc.trade(TRADES, BASE, 4)                         # stamped in the current bar
    from hermes.ibkr import raw_events as R
    sc.add(R.RawTickByTickAllLast, req_id=TRADES, tick_type=2, time=T0 + 80, price=BASE, size=5,
           past_limit=False, unreported=False, exchange="CME", special_conditions="")   # LATE print
    sc.at(T0 + 150)
    sc.error(-1, 1100)                                # outage
    sc.at(T0 + 170)
    sc.error(-1, 1102)
    sc.at(T0 + 400)                                   # empty bars in session, 5 m bar closes
    sc.tick()
    r, h = check(tmp_path, sc)
    b, hb = inst(r).bars, inst(h).bars
    for tf in (30, 60, 300):
        assert b.completed_bars(tf) == hb.completed_bars(tf) and b.completed_bars(tf)
    assert b.late_trades == 1 and b.empty_bars > 0 and b.gap_bars > 0
    flags = [x.flags for x in b.completed_bars(30)]
    assert any(f & BarFlag.LATE_DATA_OBSERVED for f in flags) and any(f & BarFlag.CONNECTION_INTERRUPTION for f in flags)
    s = r.engine.snapshot().instrument(1).session
    assert s.session.volume == sum(x.volume for x in b.completed_bars(30)) + (b.forming()[0].volume if b.forming()[0] else 0)
    assert s == h.engine.snapshot().instrument(1).session and s.gap_observed


# ============================================================================ HEALTH

def test_health_disconnect_reconnect_1101_1102_10197_delayed_generations(tmp_path):
    sc = ready()
    sc.error(-1, 1100)
    sc.error(-1, 1102)                                # restored, data kept
    sc.error(-1, 1100)
    sc.error(-1, 1101)                                # restored, data lost -> resubscribe all
    sc.subscribe(30_001, 30_002, 30_003, 30_004)
    sc.seed_book(depth=30_001, bbo=30_002, l1=30_004)
    ticks(sc, 600)
    sc.error(-1, 10197, "competing session")
    sc.control("conflict_recovery_attempt")
    sc.request("cancelMktDepth", None)
    sc.subscribe(40_001, 40_002, 40_003, 40_004)
    sc.seed_book(depth=40_001, bbo=40_002, l1=40_004)
    ticks(sc, 600)
    mid = len(sc.events)
    sc.mdt(40_004, 3)                                 # delayed data -> hard block
    sc.closed()                                       # disconnect
    sc.next_valid_id()                                # reconnect
    sc.subscribe(50_001, 50_002, 50_003, 50_004)
    sc.seed_book(depth=50_001, bbo=50_002, l1=50_004)
    ticks(sc, 600)
    r, h = check(tmp_path, sc)
    e = r.engine
    assert e.conflict.recoveries == 1 and e.conflict.phase is ConflictPhase.NONE
    assert e.connection is ConnectionState.CONNECTED and not e.not_live
    assert e.snapshot().instrument(1).market_data_ok
    assert inst(r).streams[next(iter(inst(r).streams))].requests >= 4
    kinds = {c.kind for c in r.checkpoints}
    assert any("health" in k for k in kinds) and any(c.seq > mid for c in r.checkpoints)


# ============================================================================ FILE INTEGRITY

def small(n_trades=10) -> RawScript:
    sc = ready()
    for i in range(n_trades):
        sc.advance(100)
        sc.trade(TRADES, BASE + TICK * (i % 2), 1)
    return sc


def test_clean_recording(tmp_path):
    r = replay(tmp_path, small())
    ig = r.integrity
    assert ig.replay_complete and ig.clean_close and not ig.truncated_tail and ig.problems() == []
    assert ig.label.startswith("COMPLETE (clean close")


def test_multi_part_recording_is_contiguous(tmp_path):
    sc = small()
    d = write_hrec(tmp_path / "s", sc.events, rotate_at=20)
    r = replay_session(d, ReplayOptions(policy=POLICY))
    assert r.integrity.replay_complete and len(r.info.parts) == 2
    assert r.final_hash == fp.state_hash(Harness().run(sc).engine)


def test_declared_gap_is_never_complete(tmp_path):
    sc = small()
    ev = sc.events
    kept = ev[:30] + ev[35:]
    d = write_hrec(tmp_path / "s", kept, gaps=[(30, ev[30].seq, ev[34].seq, "overflow")])
    r = replay_session(d, ReplayOptions(policy=POLICY))
    ig = r.integrity
    assert not ig.replay_complete and not ig.contiguous and ig.declared_gaps and not ig.undeclared_ranges
    assert ig.complete_through_seq == ev[29].seq and "INCOMPLETE" in ig.label
    p = replay_session(d, ReplayOptions(policy=POLICY, stop_at_gap=True))
    assert p.final_seq == ev[29].seq and p.integrity.stopped_at_seq == ev[30].seq
    assert p.final_hash == fp.state_hash(Harness().feed(ev[:30]).engine)   # the deterministic prefix


def test_missing_sequence_undeclared(tmp_path):
    sc = small()
    kept = sc.events[:20] + sc.events[22:]
    r = replay_session(write_hrec(tmp_path / "s", kept), ReplayOptions(policy=POLICY))
    ig = r.integrity
    assert not ig.replay_complete and ig.undeclared_ranges == [(21, 22)] and ig.complete_through_seq == 20


def test_truncated_final_record_keeps_complete_prefix(tmp_path):
    sc = small()
    d = write_hrec(tmp_path / "s", sc.events, final=False)
    p = d / "part-0001.hrec"
    data = p.read_bytes()
    p.write_bytes(data[:-3])                          # crash mid-write of the last record
    r = replay_session(d, ReplayOptions(policy=POLICY))
    ig = r.integrity
    assert ig.truncated_tail and not ig.clean_close and ig.contiguous and not ig.replay_complete
    assert ig.last_seq == sc.events[-2].seq and "NOT CLEANLY CLOSED" in ig.label
    assert r.final_hash == fp.state_hash(Harness().feed(sc.events[:-1]).engine)


def test_no_footer_without_truncation(tmp_path):
    r = replay_session(write_hrec(tmp_path / "s", small().events, final=False), ReplayOptions(policy=POLICY))
    assert r.integrity.contiguous and not r.integrity.clean_close and not r.integrity.truncated_tail


def test_corrupt_length_prefix_and_corrupt_payload(tmp_path):
    sc = small()
    for name, mutate in (("len", lambda b, off: b[:off] + struct.pack(">I", 0xFFFFFFF0) + b[off + 4:]),
                         ("payload", lambda b, off: b[:off + 4] + b"\xc1" * 8 + b[off + 12:])):
        d = write_hrec(tmp_path / name, sc.events)
        p = d / "part-0001.hrec"
        data = p.read_bytes()
        off = _frame_offset(data, 25)
        p.write_bytes(mutate(data, off))
        r = replay_session(d, ReplayOptions(policy=POLICY))
        ig = r.integrity
        assert ig.corrupt and not ig.contiguous and not ig.replay_complete and not ig.truncated_tail
        assert r.raw_events == 24 and ig.complete_through_seq == 24


def _frame_offset(data: bytes, index: int) -> int:
    """Byte offset of frame ``index`` (0 = header)."""
    off = len(MAGIC)
    for _ in range(index):
        (n,) = struct.unpack_from(">I", data, off)
        off += 4 + n
    return off


def test_bad_magic_is_incompatible(tmp_path):
    d = write_hrec(tmp_path / "s", small().events)
    (d / "part-0001.hrec").write_bytes(b"NOTHERMS" + b"\x00" * 20)
    with pytest.raises(ReplayIncompatible, match="magic"):
        replay_session(d)


@pytest.mark.parametrize("over,match", [({"schema_version": 99}, "schema_version"),
                                        ({"format": "other"}, "format"),
                                        ({"meta": {"normalizer_version": 99}}, "normalizer_version")])
def test_incompatible_versions_fail_clearly(tmp_path, over, match):
    d = write_hrec(tmp_path / "s", small().events, header_over=over)
    with pytest.raises(ReplayIncompatible, match=match):
        replay_session(d)


def test_unknown_raw_fields_are_never_guessed(tmp_path):
    from hermes.storage import codec
    d = write_hrec(tmp_path / "s", small().events)
    p = d / "part-0001.hrec"
    data = p.read_bytes()
    import msgpack
    off = len(MAGIC)
    (n,) = struct.unpack_from(">I", data, off)
    hdr = msgpack.unpackb(data[off + 4: off + 4 + n], raw=False, strict_map_key=False)
    code = str(codec.RAW_TYPE_CODES[type(small().events[-1])])
    key = int(code) if int(code) in hdr[1]["types"] else code
    hdr[1]["types"][key][1] = hdr[1]["types"][key][1] + ["new_field"]
    body = msgpack.packb(hdr, use_bin_type=True)
    p.write_bytes(MAGIC + struct.pack(">I", len(body)) + body + data[off + 4 + n:])
    with pytest.raises(ReplayIncompatible, match="field mismatch"):
        replay_session(d)


def test_missing_contract_metadata_is_reported(tmp_path):
    sc = RawScript(wall0=T0 * S)
    sc.next_valid_id()
    sc.trade(TRADES, BASE, 1)
    r = replay(tmp_path, sc)
    assert any("contract details" in p for p in r.integrity.problems())


# ============================================================================ DETERMINISM

def test_checkpoint_hash_sequence_stable_and_order_independent(tmp_path):
    sc = small(30)
    a = replay(tmp_path, sc, "a")
    b = replay(tmp_path, sc, "b")
    assert [c.hash for c in a.checkpoints] == [c.hash for c in b.checkpoints]
    assert len({c.hash for c in a.checkpoints}) > 3                # the hash tracks state changes
    assert fp.canon({"b": 1, "a": 2}) == fp.canon({"a": 2, "b": 1})
    assert fp.canon(frozenset({3, 1, 2})) == fp.canon(frozenset({2, 3, 1}))
    e = a.engine                                                   # dict insertion order is irrelevant
    before = fp.state_hash(e)
    e.instruments = dict(reversed(list(e.instruments.items())))
    e.counters.errors_by_code = dict(reversed(list(e.counters.errors_by_code.items())))
    assert fp.state_hash(e) == before


def test_no_wall_clock_or_randomness_dependence(tmp_path, monkeypatch):
    sc = small(20)
    d = write_hrec(tmp_path / "s", sc.events)
    ref = replay_session(d, ReplayOptions(policy=POLICY))

    def boom(*a, **k):
        raise AssertionError("replay read a system clock / RNG")
    for name in ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns"):
        monkeypatch.setattr(time, name, boom)
    monkeypatch.setattr(random, "random", boom)
    rng = iter(range(10**6, 0, -7919))                             # an absurd, non-monotonic "timer"
    r = replay_session(d, ReplayOptions(policy=POLICY, timer=lambda: next(rng)))
    assert r.final_hash == ref.final_hash
    assert [c.hash for c in r.checkpoints] == [c.hash for c in ref.checkpoints]


def test_replay_clock_exposes_recorded_time_only():
    sc = small(3)
    c = ReplayClock()
    for ev in sc.events:
        c.observe(ev)
    assert c.mono_ns() == sc.events[-1].recv_mono_ns and c.wall_ns() == sc.events[-1].recv_wall_ns
    assert c.events == len(sc.events) and c.recorded_span_ns > 0


def test_checkpointer_triggers():
    sc = small(5)
    h = Harness()
    ck = Checkpointer(h.engine, CheckpointPolicy(every_n=10, on_bar_close=False, on_health=False))
    for ev in sc.events:
        h.feed([ev])
        ck.after_raw(ev.seq)
    assert [c.seq for c in ck.checkpoints] == [s for s in range(10, len(sc.events) + 1, 10)]
    assert all(c.kind == "every_n" for c in ck.checkpoints)


def test_compare_limits_claims_to_verified_prefix(tmp_path):
    sc = small(30)
    r = replay(tmp_path, sc)
    cps = list(r.checkpoints)
    bad = cps[:-1] + [dataclasses.replace(cps[-1], hash="0" * 64)]
    assert compare_checkpoints(cps, cps, r.final, r.final).equivalent
    diff = compare_checkpoints(cps, bad)
    assert not diff.equivalent and diff.first_mismatch[0] == cps[-1].seq
    assert compare_checkpoints(cps, bad, limit_seq=cps[-2].seq).equivalent


# ============================================================================ CONFIG / TOOL

def test_recorded_config_is_used_and_explicit_config_changes_fingerprint(tmp_path):
    sc = small(5)
    rec = replay(tmp_path, sc, "a")
    cfg = dataclasses.replace(HermesConfig(), bars=BarsConfig(close_grace_ms=900))
    other = replay(tmp_path, sc, "b", config=cfg)
    assert rec.config_source == "recorded" and other.config_source == "explicit"
    assert rec.config_fingerprint != other.config_fingerprint


def test_replay_report_tool_save_verify_compare(tmp_path, capsys):
    from tools import replay_report
    d = write_hrec(tmp_path / "rec" / "2026-09-22" / "sess", small(20).events)
    out = tmp_path / "a.json"
    assert replay_report.main([str(tmp_path / "rec"), "--verify", "--save", str(out), "--every", "25"]) == 0
    text = capsys.readouterr().out
    assert "IDENTICAL" in text and "COMPLETE (clean close" in text and "30s/1m/5m" in text
    assert replay_report.main([str(d), "--compare", str(out), "--every", "25"]) == 0
    assert "EQUIVALENT" in capsys.readouterr().out
    assert load_checkpoints(out).final is not None
    assert replay_report.main([str(tmp_path / "nothing")]) == 2


# ============================================================================ SAFETY

def test_replay_runs_with_ibapi_and_network_unavailable(tmp_path):
    d = write_hrec(tmp_path / "s", small(10).events)
    code = f"""
import builtins, socket, sys
real_import = builtins.__import__
def guard(name, *a, **k):
    if name == "ibapi" or name.startswith("ibapi."):
        raise ImportError("ibapi blocked in replay safety test")
    return real_import(name, *a, **k)
builtins.__import__ = guard
def no_net(*a, **k):
    raise OSError("network blocked in replay safety test")
socket.socket.connect = no_net
socket.create_connection = no_net
from hermes.replay.runner import replay_session
r = replay_session({str(d)!r})
assert r.integrity.replay_complete and r.raw_events > 0
banned = [m for m in sys.modules if m.startswith("ibapi") or m in (
    "hermes.ibkr.adapter", "hermes.ibkr.session", "hermes.ibkr.readonly", "hermes.ibkr.gateway", "hermes.app.run_live")]
assert not banned, banned
print("OK", r.final_hash)
"""
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[2],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("OK")


def test_recording_source_is_single_pass(tmp_path):
    src = RecordingSource(write_hrec(tmp_path / "s", small(2).events))
    list(src.events())
    with pytest.raises(RuntimeError):
        list(src.events())


def test_pre_c6_recording_replays_with_current_c4_c5_logic(tmp_path):
    """A C3-era header (config without [tape]/[bars], no checkpoints) replays through today's code:
    raw-event reproducibility only, never a historical-equivalence claim."""
    cfg = dataclasses.asdict(HermesConfig())
    del cfg["tape"], cfg["bars"]
    sc = small(10)
    d = write_hrec(tmp_path / "c3", sc.events, meta={"hermes_version": "0.4.0-c3", "config": cfg})
    r = replay_session(d)
    assert r.config_source == "recorded" and r.integrity.replay_complete
    assert r.live_compare is None and "raw-event reproducibility only" in r.live_compare_status
    assert r.classified_trades == 10 and r.final_hash == fp.state_hash(Harness().run(sc).engine)


def test_live_sidecar_from_different_code_is_not_claimed_equivalent(tmp_path):
    sc = small(10)
    d = write_hrec(tmp_path / "s", sc.events)
    h = Harness()
    ck = Checkpointer(h.engine, POLICY)
    for ev in sc.events:
        h.feed([ev])
        ck.after_raw(ev.seq)
    ck.finalize()
    ck.save(d / "checkpoints.json", code_fingerprint=fp.code_fingerprint(),
            config_fingerprint=fp.config_fingerprint(HermesConfig()))
    same = replay_session(d)
    assert same.live_compare.equivalent and "EQUIVALENT" in same.live_compare_status
    ck.save(d / "checkpoints.json", code_fingerprint="0000", config_fingerprint=fp.config_fingerprint(HermesConfig()))
    other = replay_session(d)
    assert other.live_compare is None and "NO equivalence claim" in other.live_compare_status
