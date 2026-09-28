"""C8e deterministic absorption-context measurements.

This module fuses already-measured C8 structure and pattern windows. It is a PURE
derived layer: no clocks, no I/O, no mutable state, no trading signal, no score.

Important limitation:
IBKR CME depth is MBP (aggregated market-by-price), not MBO. Therefore this module
cannot prove absorption, iceberg activity, spoofing, individual order identity, or
queue position.

The key quantity is deliberately named ``compatible_cap_volume``:
it is an aggregate-window UPPER BOUND on volume that could simultaneously belong to
three observed buckets:
    1) known aggressive volume at the opposing best,
    2) replenished visible liquidity on that same side,
    3) aggressive volume with no favorable midpoint follow-through.

Because those buckets are aggregated independently inside a rolling window, their
minimum is NOT event-level matched volume and must never be reported as certain
absorbed volume.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.market.patterns import PatternSnapshot, PatternWindow
from hermes.market.structure import StructureSnapshot, StructureWindow

WINDOWS_S = (1, 5, 30)


@dataclass(frozen=True, slots=True)
class SideAbsorptionContext:
    """One aggressor-vs-resting-side context inside one rolling window."""

    aggressive_volume: int
    replenished_volume: int
    replenish_events: int
    follow_volume: int
    no_follow_volume: int
    sweep_volume: int
    sweep_events: int

    @property
    def resolved_follow_volume(self) -> int:
        return self.follow_volume + self.no_follow_volume

    @property
    def replenishment_fraction(self) -> float | None:
        return (
            None
            if self.aggressive_volume <= 0
            else min(self.replenished_volume, self.aggressive_volume) / self.aggressive_volume
        )

    @property
    def no_follow_fraction(self) -> float | None:
        resolved = self.resolved_follow_volume
        return None if resolved <= 0 else self.no_follow_volume / resolved

    @property
    def sweep_fraction(self) -> float | None:
        return (
            None
            if self.aggressive_volume <= 0
            else min(self.sweep_volume, self.aggressive_volume) / self.aggressive_volume
        )

    @property
    def compatible_cap_volume(self) -> int:
        """Aggregate upper bound, never event-level matched absorption volume."""
        return min(
            max(0, self.aggressive_volume),
            max(0, self.replenished_volume),
            max(0, self.no_follow_volume),
        )

    @property
    def sweep_compatible_cap_volume(self) -> int:
        """Upper bound requiring sweep context as a fourth aggregate bucket."""
        return min(self.compatible_cap_volume, max(0, self.sweep_volume))

    @property
    def compatible_cap_fraction(self) -> float | None:
        return (
            None
            if self.aggressive_volume <= 0
            else self.compatible_cap_volume / self.aggressive_volume
        )


@dataclass(frozen=True, slots=True)
class AbsorptionWindow:
    seconds: int

    # BUY aggressors lift ASK; ask-side replenishment/no-follow is the resting-seller context.
    buy_vs_ask: SideAbsorptionContext

    # SELL aggressors hit BID; bid-side replenishment/no-follow is the resting-buyer context.
    sell_vs_bid: SideAbsorptionContext

    edge_visibility_events: int


@dataclass(frozen=True, slots=True)
class AbsorptionSnapshot:
    available: bool
    reason: str
    structure_epoch: int
    pattern_epoch: int
    windows: tuple[AbsorptionWindow, ...]


def _by_seconds(items) -> dict[int, object]:
    return {x.seconds: x for x in items}


def _context_buy(sw: StructureWindow, pw: PatternWindow) -> SideAbsorptionContext:
    return SideAbsorptionContext(
        aggressive_volume=sw.known_buy_at_ask,
        replenished_volume=sw.replenished_ask,
        replenish_events=sw.replenish_events_ask,
        follow_volume=pw.buy_follow_volume,
        no_follow_volume=pw.buy_no_follow_volume,
        sweep_volume=pw.buy_sweep_volume,
        sweep_events=pw.buy_sweeps,
    )


def _context_sell(sw: StructureWindow, pw: PatternWindow) -> SideAbsorptionContext:
    return SideAbsorptionContext(
        aggressive_volume=sw.known_sell_at_bid,
        replenished_volume=sw.replenished_bid,
        replenish_events=sw.replenish_events_bid,
        follow_volume=pw.sell_follow_volume,
        no_follow_volume=pw.sell_no_follow_volume,
        sweep_volume=pw.sell_sweep_volume,
        sweep_events=pw.sell_sweeps,
    )


def derive_absorption_context(
    structure: StructureSnapshot,
    patterns: PatternSnapshot,
) -> AbsorptionSnapshot:
    """Fuse deterministic C8 windows without inventing missing event-level attribution."""

    if not structure.available:
        return AbsorptionSnapshot(
            available=False,
            reason=f"structure:{structure.reason}",
            structure_epoch=structure.continuity_epoch,
            pattern_epoch=patterns.continuity_epoch,
            windows=(),
        )

    if not patterns.available:
        return AbsorptionSnapshot(
            available=False,
            reason=f"patterns:{patterns.reason}",
            structure_epoch=structure.continuity_epoch,
            pattern_epoch=patterns.continuity_epoch,
            windows=(),
        )

    sw = _by_seconds(structure.windows)
    pw = _by_seconds(patterns.windows)
    missing = [w for w in WINDOWS_S if w not in sw or w not in pw]
    if missing:
        return AbsorptionSnapshot(
            available=False,
            reason="missing_windows:" + ",".join(str(x) for x in missing),
            structure_epoch=structure.continuity_epoch,
            pattern_epoch=patterns.continuity_epoch,
            windows=(),
        )

    # Epochs are independent counters with different break semantics. Equality is NOT
    # required; both sources simply need to be currently available. The epoch pair is
    # exposed so reporting/debugging can see their continuity context explicitly.
    windows = []
    for seconds in WINDOWS_S:
        s = sw[seconds]
        p = pw[seconds]
        windows.append(
            AbsorptionWindow(
                seconds=seconds,
                buy_vs_ask=_context_buy(s, p),
                sell_vs_bid=_context_sell(s, p),
                edge_visibility_events=s.edge_visibility_events,
            )
        )

    return AbsorptionSnapshot(
        available=True,
        reason="ok",
        structure_epoch=structure.continuity_epoch,
        pattern_epoch=patterns.continuity_epoch,
        windows=tuple(windows),
    )
