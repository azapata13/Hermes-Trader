#!/usr/bin/env python3
"""C7 deterministic order-flow metrics report from a Hermès .hrec recording.

Never connects to TWS. Replays the authoritative raw recording through the same
Normalizer + MarketEngine as live, then reports the latest usable pre-shutdown
C7 metric state. Final shutdown/cancellation state is shown separately.

Usage:
    python tools/metrics_report.py
    python tools/metrics_report.py <session_dir|date_dir|recordings_root>
    python tools/metrics_report.py <path> --config recorded
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
    metrics: Any
    book: Any
    grid: Any


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
    best = None
    for iid in sorted(engine.instruments):
        inst = engine.instruments[iid]
        if inst.book is None or inst.book.state is not BookState.VALID:
            continue
        ms = inst.metrics.snapshot()
        if not ms.book.available:
            continue
        best = Observation(
            seq=seq,
            label=label,
            instrument_id=iid,
            symbol=inst.local_symbol or str(iid),
            metrics=ms,
            book=inst.book.snapshot(),
            grid=inst.grid,
        )
        break
    return best


def _potential_break(raw: Any) -> bool:
    if isinstance(raw, (R.RawConnectionClosed, R.RawError, R.RawControl)):
        return True
    return isinstance(raw, R.RawRequestIssued) and raw.method.startswith("cancel")


def replay_latest_usable(session: Path, config_choice: str) -> tuple[Observation | None, MarketEngine, Any, list[str]]:
    src = RecordingSource(session)
    cfg, _cfg_source, notes = resolve_config(src.info, config_choice)
    norm = Normalizer()
    eng = MarketEngine(cfg.book, cfg.session, cfg.subscriptions, tape_cfg=cfg.tape, bars_cfg=cfg.bars)
    last_valid: Observation | None = None

    for raw in src.events():
        if _potential_break(raw):
            cap = _capture(eng, max(0, raw.seq - 1), f"before {type(raw).__name__}")
            if cap is not None:
                last_valid = cap
        try:
            for ev in norm.normalize(raw):
                eng.on_event(ev)
        except Exception as exc:  # mirror live/replay fail-safe
            eng.internal_error(f"{type(raw).__name__} seq={raw.seq}: {exc!r}", raw.recv_mono_ns)

    final_cap = _capture(eng, eng.last_seq, "final")
    if final_cap is not None:
        last_valid = final_cap
    return last_valid, eng, src.info, notes


def _ratio(v: Any) -> str:
    if v is None:
        return "n/a"
    if not v.available:
        return f"n/a ({v.bid_levels}/{v.ask_levels} of {v.requested_levels} levels)"
    return f"{v.value:+.4f}  [bid={v.bid_total} ask={v.ask_total}]"


def _price_from_units(grid: Any, units: float | None) -> str:
    if units is None:
        return "n/a"
    if grid is None:
        return f"{units:.4f} units"
    return f"{float(grid.unit) * units:.4f}"


def _mid_price(grid: Any, mid_x2: int | None) -> str:
    return _price_from_units(grid, None if mid_x2 is None else mid_x2 / 2)


def _ticks(grid: Any, delta_units: float | None) -> str:
    if delta_units is None:
        return "n/a"
    if grid is None or not grid.is_uniform:
        return f"{delta_units:+.3f} grid-units"
    step = grid.bands[0].step_units
    return f"{delta_units / step:+.3f} ticks"


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
        lines.append("metrics      no VALID C7 book observation found")
        lines.extend(f"note         {n}" for n in notes)
        return lines

    m, b, grid = obs.metrics, obs.book, obs.grid
    bm = m.book
    best_bid = b.bids[0][0] if b.bids else None
    best_ask = b.asks[0][0] if b.asks else None
    spread_ticks = None
    if grid is not None and best_bid is not None and best_ask is not None:
        try:
            spread_ticks = grid.ticks_between(best_bid, best_ask)
        except Exception:
            spread_ticks = None

    lines += [
        f"observation  seq={obs.seq} instrument={obs.symbol} source={obs.label}",
        f"quality      book={bm.available} reason={bm.reason} continuity_epoch={m.continuity_epoch}",
        f"book         bid={_price_from_units(grid, best_bid)} ask={_price_from_units(grid, best_ask)} "
        f"spread={spread_ticks if spread_ticks is not None else bm.spread_units} ticks/units",
        f"mid          {_mid_price(grid, bm.mid_x2)}",
        f"microprice   {_price_from_units(grid, bm.microprice_units)}  offset={_ticks(grid, bm.micro_offset_units)}",
        "imbalance",
        f"  L1         {_ratio(bm.l1)}",
        f"  L3         {_ratio(bm.l3)}",
        f"  L5         {_ratio(bm.l5)}",
        f"  L10        {_ratio(bm.l10)}",
        f"  weighted   {_ratio(bm.weighted)}",
        f"OFI          event={m.event_ofi if m.event_ofi is not None else 'n/a'}",
    ]

    for w in m.ofi:
        lines.append(f"  {w.seconds:>2}s        {w.value:+d}  events={w.events}")

    lines.append("trade flow")
    for f in m.trade_flow:
        kr = "n/a" if f.known_volume_ratio is None else f"{100 * f.known_volume_ratio:.1f}%"
        lines.append(
            f"  {f.seconds:>2}s        B={f.buy_volume} S={f.sell_volume} U={f.unknown_volume} "
            f"delta={f.known_delta:+d} trades={f.trade_count} known={kr}"
        )

    lines.append("velocity")
    for v in m.velocity:
        lines.append(
            f"  {v.seconds:>2}s        trades/s={v.trades_per_s:.2f} contracts/s={v.contracts_per_s:.2f} "
            f"B/s={v.buy_contracts_per_s:.2f} S/s={v.sell_contracts_per_s:.2f} "
            f"depth/s={v.depth_updates_per_s:.2f} bbo/s={v.bbo_updates_per_s:.2f}"
        )

    lines.append("price move")
    for p in m.price_move:
        mid_units = None if p.midpoint_x2_change is None else p.midpoint_x2_change / 2
        lines.append(
            f"  {p.seconds:>2}s        midpoint={_ticks(grid, mid_units)} "
            f"last_trade={_ticks(grid, p.last_trade_change)}"
        )

    for n in notes:
        lines.append(f"note         {n}")
    lines.append("note         metrics are measurements only; no directional trading signal is inferred")
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
