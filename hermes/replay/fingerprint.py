"""Deterministic MarketEngine state fingerprint (C6).

``state_summary(engine)`` builds a canonical, explicitly whitelisted summary of the deterministic
market state; ``state_hash`` serializes it canonically (sorted maps, enum values, ``repr`` for the
few config-constant floats) and hashes it with SHA-256.

Included: last raw seq, connection / farm / not-live / 10197 phase, alerts, engine counters,
per instrument (sorted by id): contract state, stream generations/status/errors, market-data
health reasons, book state/epoch/issues/all rows, BBO, last trade, L1 fields, classifier state
(epoch, current quote, history length, tick reference/direction), tape totals + latest classified
trades, bar counters + forming 30 s/1 m/5 m + latest completed bar per timeframe, quality flags,
session context (session / RTH / overnight / previous, integer VWAP accumulators).

Excluded by construction: object addresses, process clocks and anything iteration-order
dependent. C7 rolling metrics intentionally include their bounded recorded ``recv_mono_ns``
timestamps because deterministic window eviction depends on them. Every completed bar appears
in the checkpoint stream (one checkpoint per bar close), so the full bar history is covered
without hashing it wholesale each time.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Any

HASH_VERSION = 2
LATEST_TRADES = 5

# Deterministic-path sources: their content defines "same code" for live-vs-replay equivalence.
_CODE_FILES = (
    "hermes/ibkr/raw_events.py", "hermes/ibkr/normalizer.py", "hermes/ibkr/contracts.py", "hermes/ibkr/codes.py",
    "hermes/ibkr/errors.py", "hermes/ibkr/market_rules.py", "hermes/market/events.py", "hermes/market/pricegrid.py",
    "hermes/market/orderbook.py", "hermes/market/health.py", "hermes/market/classify.py", "hermes/market/tape.py",
    "hermes/market/metrics.py",
    "hermes/market/bars.py", "hermes/market/sessions.py", "hermes/market/engine.py", "hermes/market/snapshot.py",
    "hermes/replay/fingerprint.py", "hermes/replay/checkpoints.py",
)
_REPO = Path(__file__).resolve().parents[2]
ENGINE_CONFIG_SECTIONS = ("book", "session", "subscriptions", "tape", "bars")


def canon(obj: Any) -> Any:
    """Canonical JSON-able form. Enums -> value, dataclasses -> [name, fields...], maps sorted."""
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, Enum):
        v = obj.value
        return int(v) if isinstance(v, int) else v
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return ["f", repr(obj)]
    if isinstance(obj, (tuple, list)):
        return [canon(x) for x in obj]
    if isinstance(obj, dict):
        items = [[canon(k), canon(v)] for k, v in obj.items()]
        items.sort(key=lambda kv: json.dumps(kv[0], sort_keys=True))
        return ["map", items]
    if isinstance(obj, (set, frozenset)):
        return ["set", sorted((canon(x) for x in obj), key=lambda x: json.dumps(x, sort_keys=True))]
    if dataclasses.is_dataclass(obj):
        return [type(obj).__name__, [canon(getattr(obj, f.name)) for f in dataclasses.fields(obj)]]
    raise TypeError(f"not canonicalizable: {type(obj).__name__}")


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(canon(obj), separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _trade(t) -> tuple:
    return (t.seq, t.generation, t.tape_epoch, t.exch_ts_s, t.price_units, t.size, t.eligible, t.aggressor,
            t.method, t.confidence, t.unknown_reason, t.quote_bid_units, t.quote_ask_units, t.quote_seq,
            t.ref_quote_seq, t.book_valid, t.ref_quote_age_ns)


def _bar(b) -> tuple | None:
    if b is None:
        return None
    return (b.timeframe_s, b.start_s, b.open, b.high, b.low, b.close, b.volume, b.trades, b.buy_volume,
            b.sell_volume, b.unknown_volume, b.known_delta, b.vwap_num, b.first_seq, b.last_seq,
            b.excluded_trades, b.excluded_volume, int(b.flags), b.trading_date)


def _instrument(engine, inst) -> tuple:
    book = inst.book.snapshot() if inst.book is not None else None
    cl = inst.classifier
    q = cl.current_quote
    tape = inst.tape
    bars = inst.bars
    bars_part = None
    if bars is not None:
        f30, f60, f300 = bars.forming()
        bars_part = (
            bars.completed[30], bars.completed[60], bars.completed[300], bars.finalized_end_s, bars.last_price,
            bars.late_trades, bars.late_volume, bars.excluded_trades, bars.excluded_volume,
            sorted(bars.excluded_by_reason.items()), bars.empty_bars, bars.gap_bars, bars.dropped_no_price,
            bars.armed, bars.cond_flags, _bar(f30), _bar(f60), _bar(f300),
            tuple(_bar(bars.history[tf][-1]) if bars.history[tf] else None for tf in (30, 60, 300)))
    return (
        inst.instrument_id, inst.local_symbol, inst.con_id, inst.contract_state, inst.market_data_type,
        inst.mdt_generation, sorted((k.value, v) for k, v in inst.l1.items()),
        sorted((s.value, st.generation, st.status.value, st.error_active, st.last_error_code, st.events, st.requests)
               for s, st in inst.streams.items()),
        tuple(engine.market_data_reasons(inst)),
        None if book is None else (book.state, book.epoch, book.needs_resync, sorted(i.value for i in book.issues),
                                   book.stale_reason, book.bids, book.asks),
        None if inst.bbo is None else (inst.bbo.bid_units, inst.bbo.ask_units, inst.bbo.bid_size, inst.bbo.ask_size,
                                       inst.bbo.exch_ts_s),
        None if inst.last_trade is None else (inst.last_trade.price_units, inst.last_trade.size,
                                              inst.last_trade.exch_ts_s),
        (cl.epoch, None if q is None else (q.bid_units, q.ask_units, q.bid_size, q.ask_size, q.seq, q.generation),
         cl.quote_history_len, cl.tick_reference, cl.tick_direction),
        (len(tape), tape.epoch, tape.evicted_by_count, tape.evicted_by_age, tape.retained_window.frozen(),
         tape.epoch_cumulative.frozen(), tape.session_cumulative.frozen(),
         tuple(_trade(t) for t in tape.latest(LATEST_TRADES))),
        bars_part,
        inst.sessions.snapshot() if inst.sessions is not None else None,
        inst.metrics.fingerprint_state(),
    )


def state_summary(engine) -> tuple:
    c = engine.counters
    cf = engine.conflict
    return (
        "hermes-state", HASH_VERSION, engine.last_seq, engine.connection, engine.farm_broken, engine.not_live,
        engine.resubscribe_all_pending, cf.phase, cf.attempts, cf.recoveries, sorted(engine.alerts),
        (c.events, c.stale_generation_rejected, c.inactive_callbacks, c.unknown_req_id, sorted(c.anomalies.items()),
         sorted(c.errors_by_code.items()), c.resubscribe_all_requests, c.bbo_frozen_suspect),
        tuple(_instrument(engine, engine.instruments[i]) for i in sorted(engine.instruments)),
    )


def state_hash(engine) -> str:
    return digest(state_summary(engine))


def health_token(engine) -> tuple:
    """Cheap key whose change marks a health/subscription transition (checkpoint trigger).

    Change detection only (never hashed): instrument/stream iteration order is fixed by the event
    stream itself, so the same stream always yields the same trigger points.
    """
    cf = engine.conflict
    parts = [engine.connection, engine.farm_broken, engine.not_live, cf.phase, cf.attempts, tuple(engine.alerts)]
    ap = parts.append
    for inst in engine.instruments.values():
        b = inst.book
        ap(inst.contract_state)
        ap(inst.market_data_type)
        ap(None if b is None else b.state)
        for st in inst.streams.values():
            ap(st.generation)
            ap(st.status)
            ap(st.error_active)
    return tuple(parts)


def bar_count(engine) -> int:
    n = 0
    for inst in engine.instruments.values():
        b = inst.bars
        if b is not None:
            n += b.completed[30]
    return n


def compact_summary(engine) -> dict:
    """Small human-readable state digest stored next to checkpoint hashes (debugging aid only)."""
    out: dict[str, Any] = {"seq": engine.last_seq, "connection": engine.connection.value,
                           "conflict": engine.conflict.phase.value, "alerts": sorted(engine.alerts)}
    for i in sorted(engine.instruments):
        inst = engine.instruments[i]
        s = inst.tape.session_cumulative
        out[str(i)] = {
            "md_ok": not engine.market_data_reasons(inst),
            "book": None if inst.book is None else [inst.book.state.value, inst.book.epoch],
            "tape": [len(inst.tape), inst.tape.epoch, s.buy_volume, s.sell_volume, s.unknown_volume],
            "bars": None if inst.bars is None else [inst.bars.completed[tf] for tf in (30, 60, 300)],
        }
    return out


def code_fingerprint() -> str:
    """Content hash of the deterministic-path sources (independent of git state)."""
    h = hashlib.sha256()
    for rel in _CODE_FILES:
        p = _REPO / rel
        h.update(rel.encode())
        h.update(p.read_bytes() if p.exists() else b"<missing>")
    return h.hexdigest()[:16]


def config_fingerprint(cfg) -> str:
    """Hash of the engine-relevant config sections (book/session/subscriptions/tape/bars)."""
    return digest({name: getattr(cfg, name) for name in ENGINE_CONFIG_SECTIONS})[:16]
