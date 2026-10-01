from __future__ import annotations

import dataclasses
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes.ibkr import raw_events as R
from hermes.ibkr.normalizer import Normalizer
from hermes.market.engine import MarketEngine
from hermes.market.events import ConnectionState
from hermes.replay.source import RecordingSource

log = logging.getLogger("hermes.warm_start")


@dataclass(frozen=True, slots=True)
class WarmStartResult:
    used: bool
    reason: str
    session_dir: str | None = None
    age_s: float | None = None
    raw_events: int = 0
    normalized_events: int = 0
    cutoff_seq: int | None = None
    local_symbol: str | None = None
    con_id: int | None = None
    bars_30s: int = 0
    bars_1m: int = 0
    bars_5m: int = 0
    tape_size: int = 0
    session_volume: int = 0
    session_vwap: float | None = None
    trading_date: str | None = None
    # alerts the RECORDED process had raised; reported, never carried into live health
    historical_alerts: tuple[str, ...] = ()
    historical_conflict_phase: str | None = None


def _latest_session(root: Path, max_age_s: float) -> tuple[Path, float] | None:
    root = Path(root).expanduser()

    candidates = [
        p
        for p in root.glob("*/*")
        if p.is_dir() and any(p.glob("part-*.hrec"))
    ]

    if not candidates:
        return None

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    now = time.time()

    for p in candidates:
        age = max(0.0, now - p.stat().st_mtime)
        if age <= max_age_s:
            return p, age

    return None


def _last_connection_close(session_dir: Path) -> int | None:
    src = RecordingSource(session_dir)
    cutoff = None

    for raw in src.events():
        if isinstance(raw, R.RawConnectionClosed):
            cutoff = raw.seq

    return cutoff


def _expected_contract_matches(
    src: RecordingSource,
    expected_contract_spec: dict[str, Any] | None,
) -> bool:
    if expected_contract_spec is None:
        return True

    recorded = src.info.meta.get("contract_spec")

    if not isinstance(recorded, dict):
        return False

    return dict(recorded) == dict(expected_contract_spec)


def warm_start_engine(
    engine: MarketEngine,
    recording_root: str | Path,
    *,
    expected_contract_spec: dict[str, Any] | None = None,
    max_age_s: float | None = None,
    now_mono_ns: int | None = None,
) -> WarmStartResult:
    """
    Reconstruct recent market state from a local raw recording.

    Safety rules:
    - never use a recording older than max_age_s;
    - require the recorded contract spec to match the current config;
    - stop before the terminal RawConnectionClosed;
    - rebase monotonic timestamps so tape age remains meaningful after reboot;
    - leave live connection state DISCONNECTED so the normal live supervisor owns
      connection/subscription state.
    """

    if os.getenv("HERMES_WARM_START", "1").strip().lower() in {
        "0", "false", "no", "off"
    }:
        return WarmStartResult(False, "disabled")

    if max_age_s is None:
        max_age_s = float(
            os.getenv("HERMES_WARM_START_MAX_AGE_S", "43200")
        )

    latest = _latest_session(Path(recording_root), max_age_s)

    if latest is None:
        return WarmStartResult(
            False,
            f"no recording newer than {max_age_s:.0f}s",
        )

    session_dir, age_s = latest

    try:
        src = RecordingSource(session_dir)
    except Exception as exc:
        return WarmStartResult(
            False,
            f"recording unreadable: {type(exc).__name__}: {exc}",
            session_dir=str(session_dir),
            age_s=age_s,
        )

    if not _expected_contract_matches(src, expected_contract_spec):
        return WarmStartResult(
            False,
            "recording contract spec does not match current config",
            session_dir=str(session_dir),
            age_s=age_s,
        )

    # RecordingSource computes integrity while streaming and is single-pass.
    # Validate the recording first, then open a fresh source for replay.
    validator = RecordingSource(session_dir)

    try:
        for _ in validator.events():
            pass
    except Exception as exc:
        return WarmStartResult(
            False,
            f"recording validation failed: {type(exc).__name__}: {exc}",
            session_dir=str(session_dir),
            age_s=age_s,
        )

    if not validator.integrity.contiguous:
        return WarmStartResult(
            False,
            "recording has gaps/corruption; warm start refused",
            session_dir=str(session_dir),
            age_s=age_s,
        )

    cutoff_seq = _last_connection_close(session_dir)

    normalizer = Normalizer()

    first_old_mono: int | None = None
    base_new_mono = now_mono_ns or time.perf_counter_ns()

    raw_count = 0
    normalized_count = 0

    # Warm start runs before the live pipeline has acquired its dispatch-thread
    # ownership. Temporarily suspend the live single-writer guard for this
    # synchronous bootstrap replay, then restore it unconditionally.
    owner_guard = getattr(engine, "_owner_guard", None)
    engine.set_owner_guard(None)

    try:
        for raw in RecordingSource(session_dir).events():
            if cutoff_seq is not None and raw.seq >= cutoff_seq:
                break

            if first_old_mono is None:
                first_old_mono = raw.recv_mono_ns

            rebased_mono = (
                base_new_mono
                + (raw.recv_mono_ns - first_old_mono)
            )

            # Preserve wall/exchange timestamps. Only monotonic time is rebased,
            # because the new OS process has a different monotonic clock origin.
            replay_raw = dataclasses.replace(
                raw,
                recv_mono_ns=rebased_mono,
            )

            events = normalizer.normalize(replay_raw)

            for ev in events:
                engine.on_event(ev)

            raw_count += 1
            normalized_count += len(events)
    finally:
        engine.set_owner_guard(owner_guard)

    snap = engine.snapshot()
    inst = snap.instruments[0] if snap.instruments else None

    if inst is None:
        return WarmStartResult(
            False,
            "recording replay produced no instrument state",
            session_dir=str(session_dir),
            age_s=age_s,
            raw_events=raw_count,
            normalized_events=normalized_count,
            cutoff_seq=cutoff_seq,
        )

    bars = inst.bars
    tape = inst.tape
    session = inst.session

    session_stats = session.session if session is not None else None

    # Historical market state is now reconstructed, but the live supervisor
    # must own connection/subscription state from here onward. The recorded
    # process's health (alerts, 10197 budget, subscription generations, book)
    # is historical: it is reported, not carried into the new process.
    historical_conflict = engine.conflict.phase.value
    historical = engine.end_replayed_session(engine.last_mono_ns)
    if historical or historical_conflict != "none":
        log.warning(
            "WARM START | recording ended with alert(s) %s, 10197 phase=%s | historical "
            "(previous process), not carried into live health; live checks start fresh",
            ",".join(sorted(historical)) or "-", historical_conflict,
        )
    engine.connection = ConnectionState.DISCONNECTED
    engine.last_seq = 0
    engine.last_mono_ns = 0
    engine.last_wall_ns = 0

    return WarmStartResult(
        used=True,
        reason="warm replay applied",
        session_dir=str(session_dir),
        age_s=age_s,
        raw_events=raw_count,
        normalized_events=normalized_count,
        cutoff_seq=cutoff_seq,
        local_symbol=inst.local_symbol,
        con_id=inst.con_id,
        bars_30s=bars.completed_30s if bars else 0,
        bars_1m=bars.completed_1m if bars else 0,
        bars_5m=bars.completed_5m if bars else 0,
        tape_size=tape.size if tape else 0,
        session_volume=session_stats.volume if session_stats else 0,
        session_vwap=(
            session_stats.vwap
            if session_stats is not None
            else None
        ),
        trading_date=(
            session.trading_date
            if session is not None
            else None
        ),
        historical_alerts=tuple(sorted(historical)),
        historical_conflict_phase=historical_conflict,
    )
