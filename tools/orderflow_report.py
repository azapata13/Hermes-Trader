#!/usr/bin/env python3
"""C8 order-flow structure report from a Hermès .hrec recording.

Offline only. Never connects to TWS and never sends orders.

Reports the latest usable pre-shutdown C8 state:
- visible-liquidity structure / persistence / replenishment-compatible measurements
- sweep + midpoint follow-through measurements
- pure derived absorption-compatible context

Terminology is intentionally conservative because IBKR CME depth is MBP, not MBO.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes.ibkr import raw_events as R  # noqa: E402
from hermes.ibkr.normalizer import Normalizer  # noqa: E402
from hermes.market.absorption import derive_absorption_context  # noqa: E402
from hermes.market.engine import MarketEngine  # noqa: E402
from hermes.market.orderbook import BookState  # noqa: E402
from hermes.replay.runner import resolve_config  # noqa: E402
from hermes.replay.source import RecordingSource, ReplayIncompatible  # noqa: E402

DEFAULT_ROOT = Path(os.path.expanduser("~/hermes-data/recordings"))


@dataclass(frozen=True, slots=True)
class Observation:
    seq: int
    label: str
    instrument_id: int
    symbol: str
    structure: Any
    patterns: Any
    absorption: Any


def find_session(path: Path) -> Path | None:
    path = path.expanduser()
    if path.is_file():
        return path.parent
    if any(path.glob("part-*.hrec")):
        return path
    dirs = {p.parent for p in path.rglob("part-*.hrec")}
    if not dirs:
        return None
    return max(dirs, key=lambda d: max(f.stat().st_mtime for f in d.glob("part-*.hrec")))


def _capture(engine: MarketEngine, seq: int, label: str) -> Observation | None:
    for iid in sorted(engine.instruments):
        inst = engine.instruments[iid]
        if inst.book is None or inst.book.state is not BookState.VALID:
            continue
        st = inst.structure.snapshot()
        pt = inst.patterns.snapshot()
        ab = derive_absorption_context(st, pt)
        if not st.available or not pt.available or not ab.available:
            continue
        return Observation(
            seq=seq,
            label=label,
            instrument_id=iid,
            symbol=inst.local_symbol or str(iid),
            structure=st,
            patterns=pt,
            absorption=ab,
        )
    return None


def _potential_break(raw: Any) -> bool:
    if isinstance(raw, (R.RawConnectionClosed, R.RawError, R.RawControl)):
        return True
    return isinstance(raw, R.RawRequestIssued) and raw.method.startswith("cancel")


def replay_latest_usable(session: Path, config_choice: str):
    src = RecordingSource(session)
    cfg, _cfg_source, notes = resolve_config(src.info, config_choice)
    norm = Normalizer()
    eng = MarketEngine(cfg.book, cfg.session, cfg.subscriptions, tape_cfg=cfg.tape, bars_cfg=cfg.bars)
    last_valid = None

    for raw in src.events():
        if _potential_break(raw):
            cap = _capture(eng, max(0, raw.seq - 1), f"before {type(raw).__name__}")
            if cap is not None:
                last_valid = cap
        try:
            for ev in norm.normalize(raw):
                eng.on_event(ev)
        except Exception as exc:
            eng.internal_error(f"{type(raw).__name__} seq={raw.seq}: {exc!r}", raw.recv_mono_ns)

    final_cap = _capture(eng, eng.last_seq, "final")
    if final_cap is not None:
        last_valid = final_cap
    return last_valid, eng, src.info, notes


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{100.0 * v:.1f}%"


def render(obs: Observation | None, eng: MarketEngine, info: Any, notes: list[str], session: Path) -> list[str]:
    lines = [
        f"session      {session}",
        f"recorded     hermes {info.meta.get('hermes_version', '?')} git {info.meta.get('git_commit', '?')}",
    ]

    snap = eng.snapshot()
    final_inst = snap.instruments[0] if snap.instruments else None
    if final_inst is not None:
        lines.append(
            f"final        seq={snap.seq} market_data_ok={final_inst.market_data_ok} "
            f"book={final_inst.book.state.value if final_inst.book else 'none'}"
        )

    if obs is None:
        lines.append("C8           no usable VALID structure/pattern observation found")
        lines.extend(f"note         {n}" for n in notes)
        return lines

    st, pt, ab = obs.structure, obs.patterns, obs.absorption
    lines += [
        f"observation  seq={obs.seq} instrument={obs.symbol} source={obs.label}",
        f"continuity   structure_epoch={st.continuity_epoch} pattern_epoch={pt.continuity_epoch}",
        "structure",
    ]

    st_by = {w.seconds: w for w in st.windows}
    for sec in (1, 5, 30):
        w = st_by[sec]
        lines.append(
            f"  {sec:>2}s        add(B/A)={w.added_bid}/{w.added_ask} "
            f"remove(B/A)={w.removed_bid}/{w.removed_ask} "
            f"replenish(B/A)={w.replenished_bid}/{w.replenished_ask} "
            f"hits buy@ask={w.known_buy_at_ask} sell@bid={w.known_sell_at_bid} "
            f"edge={w.edge_visibility_events}"
        )

    lines.append("patterns")
    pt_by = {w.seconds: w for w in pt.windows}
    for sec in (1, 5, 30):
        w = pt_by[sec]
        lines.append(
            f"  {sec:>2}s        sweeps B/S={w.buy_sweeps}/{w.sell_sweeps} "
            f"vol B/S={w.buy_sweep_volume}/{w.sell_sweep_volume} max_levels={w.max_sweep_levels} "
            f"follow_events={w.follow_events} no_follow_events={w.no_follow_events}"
        )

    lines.append("absorption-compatible context")
    for w in ab.windows:
        b = w.buy_vs_ask
        s = w.sell_vs_bid
        lines.append(
            f"  {w.seconds:>2}s BUY->ASK  aggressive={b.aggressive_volume} replenished={b.replenished_volume} "
            f"no_follow={b.no_follow_volume} cap={b.compatible_cap_volume} "
            f"cap%={_pct(b.compatible_cap_fraction)} sweep_cap={b.sweep_compatible_cap_volume}"
        )
        lines.append(
            f"      SELL->BID aggressive={s.aggressive_volume} replenished={s.replenished_volume} "
            f"no_follow={s.no_follow_volume} cap={s.compatible_cap_volume} "
            f"cap%={_pct(s.compatible_cap_fraction)} sweep_cap={s.sweep_compatible_cap_volume}"
        )

    for n in notes:
        lines.append(f"note         {n}")
    lines += [
        "note         compatible_cap is an aggregate upper bound, not matched absorbed volume",
        "note         MBP cannot prove iceberg/spoofing, individual order identity, or queue position",
        "note         C8 measurements do not infer a directional trading signal",
    ]
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=str(DEFAULT_ROOT))
    ap.add_argument("--config", default="recorded", help="recorded | current | <path to toml>")
    args = ap.parse_args(argv)

    session = find_session(Path(args.path))
    if session is None:
        print(f"No recording found under {args.path}", file=sys.stderr)
        return 2

    try:
        obs, eng, info, notes = replay_latest_usable(session, args.config)
    except ReplayIncompatible as exc:
        print(f"INCOMPATIBLE RECORDING: {exc}", file=sys.stderr)
        return 1

    print("\n".join(render(obs, eng, info, notes, session)))
    return 0 if obs is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
