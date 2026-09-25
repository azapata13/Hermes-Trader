"""Authoritative deterministic replay (C6).

    RecordingSource (raw .hrec)  ->  Normalizer  ->  MarketEngine  ->  snapshots / checkpoints

The replay source replaces IBKR, never the engine: the SAME ``Normalizer`` and ``MarketEngine``
(order book, BBO, classifier, tape, bars, sessions, health) run here as in live processing. No
TWS connection, no ibapi client, no requests, no sockets; engine time comes from recorded event
fields only (``ReplayClock``).

Modes
-----
* FAST  : as fast as possible (tests, metrics, later strategy research).
* PACED : sleeps to reproduce recorded inter-event timing divided by ``speed``. Wall time is used
          ONLY to decide how long to sleep; it never reaches the engine, so FAST and PACED always
          produce identical state and checkpoint sequences.

Equivalence claims
------------------
A. raw-event reproducibility: same raw bytes (``raw_digest``) -> same checkpoint sequence, always.
B. same-code live-vs-replay equivalence: only when the session has live checkpoints
   (``checkpoints.json``) produced by the same deterministic code (``code_fingerprint``), the same
   engine config and hash version — and only up to the contiguous recorded prefix.
"""

from __future__ import annotations

import dataclasses
import resource
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from itertools import islice
from pathlib import Path
from typing import Any, Callable

from hermes.config import ConfigError, HermesConfig, config_from_mapping, load_config
from hermes.ibkr.normalizer import NORMALIZER_VERSION, Normalizer
from hermes.market.engine import MarketEngine
from hermes.replay import fingerprint as fp
from hermes.replay.checkpoints import (
    SIDECAR_NAME,
    Checkpoint,
    Checkpointer,
    CheckpointPolicy,
    CompareResult,
    compare_checkpoints,
    load_checkpoints,
)
from hermes.replay.clock import ReplayClock
from hermes.replay.source import Integrity, RecordingInfo, RecordingSource


class ReplayMode(str, Enum):
    FAST = "fast"
    PACED = "paced"


@dataclass(slots=True)
class ReplayOptions:
    mode: ReplayMode = ReplayMode.FAST
    speed: float = 1.0                         # PACED only: 2.0 = twice real time, 0.5 = half speed
    config: str | HermesConfig = "recorded"    # "recorded" | "current" | path | HermesConfig
    policy: CheckpointPolicy | None = None     # None: the live sidecar's policy if present, else default
    stop_at_gap: bool = False                  # replay only the deterministic prefix
    max_sleep_s: float | None = None           # PACED: cap idle gaps (debug convenience)
    compare_live: bool = True                  # compare with <session>/checkpoints.json when present
    sleeper: Callable[[float], None] = time.sleep
    timer: Callable[[], int] = time.perf_counter_ns     # measures elapsed/pacing only, never engine time


class Pacer:
    """Real-time pacing for PACED mode. Only ever sleeps; never alters events."""

    def __init__(self, speed: float, timer: Callable[[], int], sleeper: Callable[[float], None],
                 max_sleep_s: float | None) -> None:
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self.speed, self.timer, self.sleeper, self.max_sleep_s = speed, timer, sleeper, max_sleep_s
        self._rec0: int | None = None
        self._t0 = 0
        self.slept_s = 0.0

    def wait(self, recorded_mono_ns: int) -> None:
        if self._rec0 is None:
            self._rec0, self._t0 = recorded_mono_ns, self.timer()
            return
        target = self._t0 + (recorded_mono_ns - self._rec0) / self.speed
        delay = (target - self.timer()) / 1e9
        if delay <= 0:
            return
        if self.max_sleep_s is not None and delay > self.max_sleep_s:
            self._t0 -= int((delay - self.max_sleep_s) * 1e9)      # skip the idle gap
            delay = self.max_sleep_s
        self.sleeper(delay)
        self.slept_s += delay


@dataclass(slots=True)
class ReplayResult:
    session_dir: str
    session_id: str
    mode: str
    speed: float
    config_source: str
    info: RecordingInfo
    integrity: Integrity
    first_seq: int | None
    final_seq: int | None
    raw_events: int
    normalized_events: int
    internal_errors: int
    elapsed_s: float
    raw_per_s: float
    events_per_s: float
    checkpoint_s: float
    peak_rss_mb: float
    code_fingerprint: str
    config_fingerprint: str
    final_hash: str
    checkpoints: list[Checkpoint]
    final: Checkpoint
    market_data_ok: bool
    not_ok_reasons: tuple[str, ...]
    book_state: str | None
    classified_trades: int
    buy_volume: int
    sell_volume: int
    unknown_volume: int
    bars: tuple[int, int, int]
    session_available: bool
    session: Any                                # SessionSnapshot | None
    live_compare: CompareResult | None = None
    live_compare_status: str = ""
    notes: list[str] = field(default_factory=list)
    policy: CheckpointPolicy | None = None
    engine: Any = None                          # the replayed MarketEngine (not serialized)

    # convenience
    @property
    def replay_complete(self) -> bool:
        return self.integrity.replay_complete

    @property
    def gaps(self) -> list:
        return list(self.integrity.declared_gaps)

    @property
    def clean_close(self) -> bool:
        return self.integrity.clean_close

    @property
    def checkpoint_count(self) -> int:
        return len(self.checkpoints)

    def to_dict(self) -> dict:
        """JSON-able summary (used by ``tools/replay_report.py --save/--compare``)."""
        ig = self.integrity
        return {
            "format": "hermes-checkpoints", "version": 1, "hash_version": fp.HASH_VERSION, "source": "replay",
            "session_dir": self.session_dir, "session_id": self.session_id, "mode": self.mode,
            "config_source": self.config_source, "code_fingerprint": self.code_fingerprint,
            "config_fingerprint": self.config_fingerprint, "normalizer_version": NORMALIZER_VERSION,
            "raw_digest": ig.raw_digest, "replay_complete": ig.replay_complete, "contiguous": ig.contiguous,
            "complete_through_seq": ig.complete_through_seq, "raw_events": self.raw_events,
            "last_seq": self.final_seq, "dropped": 0,
            "policy": dataclasses.asdict(self.policy or CheckpointPolicy()),
            "checkpoints": [[c.seq, c.raw_count, c.kind, c.hash] for c in self.checkpoints],
            "final": [self.final.seq, self.final.raw_count, "final", self.final.hash],
        }


def resolve_config(info: RecordingInfo, choice: str | HermesConfig) -> tuple[HermesConfig, str, list[str]]:
    notes: list[str] = []
    if isinstance(choice, HermesConfig):
        return choice, "explicit", notes
    if choice == "recorded":
        rec = info.config
        if rec is not None:
            try:
                return config_from_mapping(rec), "recorded", notes
            except (ConfigError, TypeError) as exc:
                notes.append(f"recorded config not loadable with current code ({exc}); using current config file")
        else:
            notes.append("recording has no config snapshot; using current config file")
        return load_config(), "current", notes
    if choice == "current":
        return load_config(), "current", notes
    return load_config(choice), f"file:{choice}", notes


def replay_session(path: str | Path, options: ReplayOptions | None = None) -> ReplayResult:
    opt = options or ReplayOptions()
    src = RecordingSource(Path(path))
    cfg, cfg_source, notes = resolve_config(src.info, opt.config)

    live = None
    sidecar = src.session_dir / SIDECAR_NAME
    if opt.compare_live and sidecar.exists():
        try:
            live = load_checkpoints(sidecar)
        except (ValueError, KeyError, TypeError) as exc:
            notes.append(f"{SIDECAR_NAME} unreadable: {exc}")
    policy = opt.policy or (live.policy if live is not None else CheckpointPolicy())

    norm = Normalizer()
    eng = MarketEngine(cfg.book, cfg.session, cfg.subscriptions, tape_cfg=cfg.tape, bars_cfg=cfg.bars)
    timer = opt.timer
    ck = Checkpointer(eng, policy, timer=timer)
    clock = ReplayClock()
    pacer = Pacer(opt.speed, timer, opt.sleeper, opt.max_sleep_s) if opt.mode is ReplayMode.PACED else None
    n_events = 0
    internal_errors = 0
    normalize, on_event, observe, after_raw = norm.normalize, eng.on_event, clock.observe, ck.after_raw
    t0 = timer()
    for raw in _batched(src.events(stop_at_gap=opt.stop_at_gap)):
        observe(raw)
        if pacer is not None:
            pacer.wait(raw.recv_mono_ns)
        try:                                   # mirrors RawPipeline._process exactly
            events = normalize(raw)
            for ev in events:
                on_event(ev)
            n_events += len(events)
        except Exception as exc:  # noqa: BLE001 - same fail-safe as live (a bug replays identically)
            internal_errors += 1
            eng.internal_error(f"{type(raw).__name__} seq={raw.seq}: {exc!r}", raw.recv_mono_ns)
        after_raw(raw.seq)
    final = ck.finalize()
    elapsed = (timer() - t0) / 1e9
    ig = src.integrity

    snap = eng.snapshot()
    inst = snap.instruments[0] if snap.instruments else None
    tape = inst.tape if inst is not None else None
    bars = inst.bars if inst is not None else None
    sess = inst.session if inst is not None else None
    res = ReplayResult(
        session_dir=str(src.session_dir), session_id=src.info.session_id, mode=opt.mode.value,
        speed=opt.speed, config_source=cfg_source, info=src.info, integrity=ig,
        first_seq=ig.first_seq, final_seq=ig.last_seq if ig.stopped_at_seq is None else ck.last_seq,
        raw_events=ck.raw_count, normalized_events=n_events, internal_errors=internal_errors,
        elapsed_s=elapsed, raw_per_s=ck.raw_count / elapsed if elapsed > 0 else 0.0,
        events_per_s=n_events / elapsed if elapsed > 0 else 0.0, checkpoint_s=ck.hash_ns / 1e9,
        peak_rss_mb=_peak_rss_mb(), code_fingerprint=fp.code_fingerprint(), config_fingerprint=fp.config_fingerprint(cfg),
        final_hash=final.hash, checkpoints=list(ck.checkpoints), final=final,
        market_data_ok=bool(inst and inst.market_data_ok), not_ok_reasons=inst.not_ok_reasons if inst else (),
        book_state=inst.book.state.value if inst and inst.book else None,
        classified_trades=tape.session_cumulative.buy_trades + tape.session_cumulative.sell_trades
        + tape.session_cumulative.unknown_trades if tape else 0,
        buy_volume=tape.session_cumulative.buy_volume if tape else 0,
        sell_volume=tape.session_cumulative.sell_volume if tape else 0,
        unknown_volume=tape.session_cumulative.unknown_volume if tape else 0,
        bars=(bars.completed_30s, bars.completed_1m, bars.completed_5m) if bars else (0, 0, 0),
        session_available=bool(sess and sess.calendar_ok), session=sess, notes=notes, policy=policy, engine=eng)
    if internal_errors:
        notes.append(f"{internal_errors} processing error(s) (engine fail-safe applied, as live would)")
    if ck.dropped:
        notes.append(f"{ck.dropped} checkpoint(s) dropped (max_checkpoints)")
    _compare_live(res, live, cfg, ck)
    return res


def _compare_live(res: ReplayResult, live, cfg: HermesConfig, ck: Checkpointer) -> None:
    if live is None:
        res.live_compare_status = ("no live checkpoints (recording predates C6, or live run did not shut down "
                                   "cleanly): raw-event reproducibility only")
        return
    m = live.meta
    reasons = []
    if m.get("hash_version") != fp.HASH_VERSION:
        reasons.append(f"hash_version {m.get('hash_version')} != {fp.HASH_VERSION}")
    if m.get("code_fingerprint") != res.code_fingerprint:
        reasons.append(f"code {m.get('code_fingerprint')} != {res.code_fingerprint}")
    if m.get("config_fingerprint") != res.config_fingerprint:
        reasons.append(f"engine config {m.get('config_fingerprint')} != {res.config_fingerprint}")
    if m.get("dropped") or ck.dropped:
        reasons.append("checkpoints were dropped")
    if reasons:
        res.live_compare_status = ("different code/config than the live run — NO equivalence claim "
                                   "(raw-event reproducibility only): " + "; ".join(reasons))
        return
    ig = res.integrity
    limit = None if ig.replay_complete else ig.complete_through_seq
    r = compare_checkpoints(live.checkpoints, res.checkpoints, live.final, res.final, limit_seq=limit)
    res.live_compare = r
    scope = "full recording" if limit is None else f"verified prefix through seq {limit} (recording not complete)"
    verdict = "EQUIVALENT" if r.equivalent else "MISMATCH"
    res.live_compare_status = (f"same-code live vs replay: {verdict} — {r.matched}/{r.compared} checkpoints, "
                               f"final {'match' if r.final_match else ('n/a' if not r.final_compared else 'DIFF')}; "
                               f"{scope}" + (f"; first mismatch {r.first_mismatch[:2]}" if r.first_mismatch else ""))


def _batched(it, n: int = 256):
    """Decode in small batches before processing: same order, same events, ~25% faster in CPython
    (better cache locality between msgpack decoding and engine work)."""
    while True:
        batch = list(islice(it, n))
        if not batch:
            return
        yield from batch


def _peak_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024
