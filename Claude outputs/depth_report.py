#!/usr/bin/env python3
"""Depth-health forensics on a raw recording (deterministic replay, never touches TWS).

    python tools/depth_report.py <session_dir>            # e.g. ~/hermes-data/recordings/2026-09-25/<id>

Replays the recording through the SAME Normalizer + MarketEngine and reports, for every depth
generation (initial subscription and each resync):

  * how it ended: the invalidation reason (structural violation kind, persistent crossed / unsorted /
    BBO mismatch, data anomaly, 317, disconnect ...), the exact raw event (seq, op, side, position,
    price, size, L2 flag) and the row counts / top of book / tick-by-tick BBO just before it;
  * the last depth callbacks before the failure;
  * book state transitions, time to first data, max rows per side, whether VALID was ever reached;
  * elapsed time between resyncs, old-generation callbacks after each resync, 317s.

Plus global evidence: depth op mix (insert/update/delete per side, L1 vs L2 callbacks), position
distribution, and book-top vs BBO differences (how often and by how many ticks they disagree).
Read-only; output is deterministic for a given recording + config.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes.ibkr import raw_events as R  # noqa: E402
from hermes.ibkr.normalizer import Normalizer  # noqa: E402
from hermes.market import events as M  # noqa: E402
from hermes.market.engine import MarketEngine  # noqa: E402
from hermes.market.events import Stream  # noqa: E402
from hermes.replay.runner import resolve_config  # noqa: E402
from hermes.replay.source import RecordingSource  # noqa: E402

OPS = {0: "INSERT", 1: "UPDATE", 2: "DELETE"}
SIDES = {0: "ASK", 1: "BID"}


@dataclass
class Generation:
    req_id: int
    requested_seq: int
    requested_ns: int
    cause: str = "initial"
    first_data_ns: int | None = None
    depth_events: int = 0
    ops: Counter = field(default_factory=Counter)
    max_rows: list = field(default_factory=lambda: [0, 0])
    reached_valid: bool = False
    transitions: list = field(default_factory=list)
    end: dict | None = None


def fmt_ms(ns: int | None) -> str:
    return "-" if ns is None else f"{ns / 1e6:,.0f} ms"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session")
    ap.add_argument("--config", default="recorded")
    ap.add_argument("--context", type=int, default=12, help="depth callbacks shown before each failure")
    args = ap.parse_args(argv)

    src = RecordingSource(Path(args.session))
    cfg, cfg_src, _ = resolve_config(src.info, args.config)
    norm = Normalizer()
    eng = MarketEngine(cfg.book, cfg.session, cfg.subscriptions, tape_cfg=cfg.tape, bars_cfg=cfg.bars)

    gens: list[Generation] = []
    by_req: dict[int, Generation] = {}
    recent: deque = deque(maxlen=args.context)
    op_mix: Counter = Counter()
    positions: Counter = Counter()
    l2 = Counter()
    top_vs_bbo: Counter = Counter()
    old_gen_callbacks: Counter = Counter()
    errors: list = []
    anomalies: Counter = Counter()
    last_inv = 0
    last_tr = 0
    controls: list = []
    first_ok_seq = None

    for raw in src.events():
        t = type(raw)
        book = None
        inst = eng.instruments.get(1)
        if inst is not None:
            book = inst.book
        pre = None
        if book is not None:
            b, a = book.best_bid(), book.best_ask()
            pre = dict(rows=(len(book.levels(M.BookSide.BID)), len(book.levels(M.BookSide.ASK))),
                       top=(b[0] if b else None, a[0] if a else None), state=book.state.value,
                       issues=sorted(i.value for i in book.issues),
                       bbo=(inst.bbo.bid_units, inst.bbo.ask_units) if inst.bbo else None)
        if t is R.RawRequestIssued and raw.method == "reqMktDepth":
            g = Generation(raw.req_id, raw.seq, raw.recv_mono_ns)
            if gens:
                prev = gens[-1]
                g.cause = (prev.end or {}).get("reason", "unknown (no invalidation seen)")
            gens.append(g)
            by_req[raw.req_id] = g
        if t is R.RawMarketDepth:
            op_mix[(OPS.get(raw.operation, raw.operation), SIDES.get(raw.side, raw.side))] += 1
            positions[raw.position] += 1
            l2["L2" if raw.is_l2 else "L1"] += 1
            recent.append(raw)
            g = by_req.get(raw.req_id)
            cur = gens[-1] if gens else None
            if g is not None and cur is not None and g is not cur:
                old_gen_callbacks[raw.req_id] += 1
            elif g is not None:
                g.depth_events += 1
                g.ops[OPS.get(raw.operation, raw.operation)] += 1
                if g.first_data_ns is None:
                    g.first_data_ns = raw.recv_mono_ns
        if t is R.RawError and raw.code in (317, 316, 322, 10092, 2152):
            errors.append((raw.seq, raw.req_id, raw.code, raw.message))
        if t is R.RawControl:
            controls.append((raw.seq, raw.kind, raw.detail))

        evs = norm.normalize(raw)
        for ev in evs:
            if isinstance(ev, M.DataAnomalyEvent) and ev.stream is Stream.DEPTH:
                anomalies[(ev.kind.value, ev.detail[:60])] += 1
            eng.on_event(ev)

        inst = eng.instruments.get(1)
        if inst is None or inst.book is None:
            continue
        book = inst.book
        if first_ok_seq is None and not eng.market_data_reasons(inst):
            first_ok_seq = raw.seq
        cur = gens[-1] if gens else None
        # transitions
        n_tr = book.counters.transitions
        if n_tr != last_tr:
            new = list(book.transitions)[-(n_tr - last_tr):]
            for tr in new:
                if cur is not None:
                    cur.transitions.append((raw.seq, tr.from_state.value, tr.to_state.value, tr.cause))
                    if tr.to_state.value == "valid":
                        cur.reached_valid = True
            last_tr = n_tr
        if cur is not None:
            cur.max_rows[0] = max(cur.max_rows[0], len(book.levels(M.BookSide.BID)))
            cur.max_rows[1] = max(cur.max_rows[1], len(book.levels(M.BookSide.ASK)))
        # book top vs BBO
        b, a = book.best_bid(), book.best_ask()
        if b and a and inst.bbo and inst.bbo.bid_units is not None and inst.bbo.ask_units is not None:
            top_vs_bbo[(b[0] - inst.bbo.bid_units, a[0] - inst.bbo.ask_units)] += 1
        # invalidations
        n_inv = sum(book.counters.invalidations.values())
        if n_inv != last_inv:
            last_inv = n_inv
            viol = dict(book.counters.violations)
            info = dict(seq=raw.seq, ns=raw.recv_mono_ns, raw_type=t.__name__,
                        reason=book.stale_reason.value if book.stale_reason else "?",
                        violations={k.value: v for k, v in viol.items()}, pre=pre)
            if t is R.RawMarketDepth:
                info["op"] = (f"{OPS.get(raw.operation, raw.operation)} {SIDES.get(raw.side, raw.side)} "
                              f"pos={raw.position} price={raw.price} size={raw.size} "
                              f"{'L2' if raw.is_l2 else 'L1'} reqId={raw.req_id}")
            if t is R.RawError:
                info["op"] = f"error {raw.code} reqId={raw.req_id}: {raw.message}"
            info["recent"] = [(r.seq, OPS.get(r.operation), SIDES.get(r.side), r.position, r.price, str(r.size),
                               r.req_id, "L2" if r.is_l2 else "L1") for r in recent]
            if cur is not None and cur.end is None:
                cur.end = info

    # ------------------------------------------------------------------ report
    ig = src.integrity
    print(f"session {src.session_dir}  ({ig.label}; {ig.raw_records} raw; config {cfg_src})")
    print(f"first market_data_ok seq: {first_ok_seq}   final book: {eng.instruments[1].book.state.value if 1 in eng.instruments and eng.instruments[1].book else None}")
    print(f"depth callbacks: {dict(l2)}  op mix: {dict(sorted(op_mix.items()))}")
    print(f"positions: {dict(sorted(positions.items()))}")
    if anomalies:
        print(f"depth anomalies: {dict(anomalies)}")
    tops = sum(top_vs_bbo.values()) or 1
    agree = top_vs_bbo.get((0, 0), 0)
    print(f"book top vs BBO (bid diff, ask diff in ticks) after events: agree {100 * agree / tops:.1f}%  "
          f"most common: {top_vs_bbo.most_common(8)}")
    for e in errors:
        print(f"error seq={e[0]} reqId={e[1]} code={e[2]}: {e[3]}")
    for c in controls:
        if "resync" in c[1] or "conflict" in c[1]:
            print(f"control seq={c[0]} {c[1]} {c[2]}")
    prev_ns = None
    for i, g in enumerate(gens):
        label = "initial subscription" if i == 0 else f"resync #{i}"
        print(f"\n{label}: reqId={g.req_id} requested seq={g.requested_seq}"
              + (f" (+{fmt_ms(g.requested_ns - prev_ns)} after previous request)" if prev_ns else ""))
        prev_ns = g.requested_ns
        print(f"  trigger: {g.cause}")
        print(f"  first data after {fmt_ms(None if g.first_data_ns is None else g.first_data_ns - g.requested_ns)}; "
              f"{g.depth_events} depth events {dict(g.ops)}; max rows bid/ask {g.max_rows}; "
              f"reached VALID: {g.reached_valid}; old-generation callbacks for this reqId after replacement: "
              f"{old_gen_callbacks.get(g.req_id, 0)}")
        for tr in g.transitions[:10]:
            print(f"    seq {tr[0]}: {tr[1]} -> {tr[2]} ({tr[3]})")
        e = g.end
        if e is None:
            print("  ended: no invalidation")
            continue
        p = e["pre"] or {}
        print(f"  ended: seq {e['seq']} {e['reason']} after {fmt_ms(e['ns'] - g.requested_ns)} "
              f"via {e['raw_type']} {e.get('op', '')}")
        print(f"    before: state={p.get('state')} rows bid/ask={p.get('rows')} top={p.get('top')} "
              f"bbo={p.get('bbo')} issues={p.get('issues')} violations(total)={e['violations']}")
        for r in e["recent"]:
            print(f"      depth seq {r[0]}: {r[1]} {r[2]} pos={r[3]} price={r[4]} size={r[5]} reqId={r[6]} {r[7]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
