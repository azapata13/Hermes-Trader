"""Immutable market snapshots (C3 subset) and the atomic snapshot publisher.

Snapshots are built on the dispatch thread (single writer) and published by reference swap;
any thread may read ``SnapshotPublisher.latest()``. Strategy code (later) consumes snapshots,
never raw callbacks.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.market.events import ConnectionState, Stream, StreamStatus
from hermes.market.orderbook import BookSnapshot
from hermes.market.tape import TapeSnapshot


@dataclass(frozen=True, slots=True)
class StreamSnapshot:
    stream: Stream
    generation: int | None
    status: StreamStatus            # EFFECTIVE status (blocks/connection/farm applied)
    last_event_mono_ns: int | None  # age = now - last_event (telemetry only)
    events: int
    requests: int
    last_error_code: int | None


@dataclass(frozen=True, slots=True)
class BboSnapshot:
    bid_units: int | None
    ask_units: int | None
    bid_size: int
    ask_size: int
    exch_ts_s: int
    recv_mono_ns: int


@dataclass(frozen=True, slots=True)
class TradeSnapshot:
    price_units: int
    size: int
    exch_ts_s: int
    recv_mono_ns: int


@dataclass(frozen=True, slots=True)
class InstrumentSnapshot:
    instrument_id: int
    local_symbol: str
    con_id: int | None
    contract_state: str             # "pending" | "resolved" | "defined" | "failed"
    book: BookSnapshot | None
    bbo: BboSnapshot | None
    last_trade: TradeSnapshot | None
    market_data_type: int | None    # for the ACTIVE L1 generation
    streams: tuple[StreamSnapshot, ...]
    market_data_ok: bool
    not_ok_reasons: tuple[str, ...]
    tape: TapeSnapshot | None = None   # C4 compact view (latest N trades + totals), never the full tape


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    seq: int                        # last raw seq applied
    mono_ns: int
    wall_ns: int
    connection: ConnectionState
    farm_broken: bool
    conflict_phase: str
    conflict_attempts: int
    not_live: bool
    alerts: tuple[str, ...]         # active critical alerts
    instruments: tuple[InstrumentSnapshot, ...]

    def instrument(self, instrument_id: int) -> InstrumentSnapshot | None:
        for i in self.instruments:
            if i.instrument_id == instrument_id:
                return i
        return None


class SnapshotPublisher:
    """Latest-value publication. Readers never block the writer."""

    __slots__ = ("_latest", "published")

    def __init__(self) -> None:
        self._latest: MarketSnapshot | None = None
        self.published = 0

    def publish(self, snapshot: MarketSnapshot) -> None:
        self._latest = snapshot          # atomic reference assignment
        self.published += 1

    def latest(self) -> MarketSnapshot | None:
        return self._latest
