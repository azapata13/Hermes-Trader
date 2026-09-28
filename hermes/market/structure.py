"""C8 market-structure measurements for aggregated MBP depth.

This module is measurement-only. It does NOT produce trade signals, strategy scores,
or order decisions.

Important market-data limitation:
IBKR CME market depth is aggregated market-by-price (MBP), not market-by-order (MBO).
Therefore:
- a displayed size decrease is "visible liquidity removed", not automatically a cancellation;
- a size increase after an aggressive trade is a "replenishment-compatible" event, not proof
  of an iceberg or the identity of any individual order;
- queue position and individual-order identity are unknowable here.

All time comes from recorded/live ``recv_mono_ns`` supplied by the caller. No clocks, I/O,
threads, randomness, numpy, pandas, or network access.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable

from hermes.market.classify import Aggressor
from hermes.market.events import BookSide
from hermes.market.orderbook import BookSnapshot, BookState, LevelChange
from hermes.market.tape import ClassifiedTrade

_S = 1_000_000_000
WINDOWS_S = (1, 5, 30)


@dataclass(frozen=True, slots=True)
class LevelPersistence:
    side: BookSide
    price_units: int
    size: int
    age_ns: int
    peak_size: int
    visible_additions: int
    visible_removals: int
    replenishment_events: int
    replenished_volume: int


@dataclass(frozen=True, slots=True)
class StructureWindow:
    seconds: int
    added_bid: int
    removed_bid: int
    added_ask: int
    removed_ask: int
    replenished_bid: int
    replenished_ask: int
    replenish_events_bid: int
    replenish_events_ask: int
    known_buy_at_ask: int
    known_sell_at_bid: int
    edge_visibility_events: int

    @property
    def net_bid_displayed(self) -> int:
        return self.added_bid - self.removed_bid

    @property
    def net_ask_displayed(self) -> int:
        return self.added_ask - self.removed_ask

    @property
    def replenished_total(self) -> int:
        return self.replenished_bid + self.replenished_ask


@dataclass(frozen=True, slots=True)
class StructureSnapshot:
    continuity_epoch: int
    available: bool
    reason: str
    current_levels: tuple[LevelPersistence, ...]
    windows: tuple[StructureWindow, ...]


@dataclass(slots=True)
class _Level:
    side: BookSide
    price_units: int
    size: int
    first_seen_ns: int
    peak_size: int
    visible_additions: int = 0
    visible_removals: int = 0
    replenishment_events: int = 0
    replenished_volume: int = 0


@dataclass(slots=True)
class _Hit:
    at_ns: int
    side: BookSide
    price_units: int
    remaining: int


# (time, side, added, removed, replenished, replenish_event, edge_visibility)
_LiqEvent = tuple[int, BookSide, int, int, int, int, int]
# (time, aggressive_side, volume_at_current_opposing_best)
_HitEvent = tuple[int, Aggressor, int]


class StructureEngine:
    """Deterministic per-instrument visible-liquidity structure tracker.

    ``replenish_link_ms`` is intentionally a narrow attribution window. A replenishment
    candidate is counted only when newly displayed size at the SAME price can be matched
    to known aggressive volume at that price within this window. Matching is conservative:
    one unit of aggressive volume can be consumed at most once.

    The result remains "replenishment-compatible", never proof of an iceberg.
    """

    __slots__ = (
        "instrument_id", "continuity_epoch", "_available", "_reason", "_now_ns",
        "_levels", "_liq_events", "_hit_events", "_recent_hits", "_link_ns",
    )

    def __init__(self, instrument_id: int, *, replenish_link_ms: int = 250) -> None:
        if replenish_link_ms < 0:
            raise ValueError("replenish_link_ms must be >= 0")
        self.instrument_id = instrument_id
        self.continuity_epoch = 0
        self._available = False
        self._reason = "not_observed"
        self._now_ns = 0
        self._levels: dict[tuple[BookSide, int], _Level] = {}
        self._liq_events: deque[_LiqEvent] = deque()
        self._hit_events: deque[_HitEvent] = deque()
        self._recent_hits: deque[_Hit] = deque()
        self._link_ns = replenish_link_ms * 1_000_000

    # ---------------------------------------------------------------- book
    def observe_book(
        self,
        book: BookSnapshot,
        changes: Iterable[LevelChange],
        now_ns: int,
    ) -> None:
        self._advance_clock(now_ns)

        if book.state is not BookState.VALID or not book.bids or not book.asks:
            reason = f"book_{book.state.value}"
            if self._available:
                self.continuity_epoch += 1
                self._clear(reason)
            else:
                self._reason = reason
            self._evict()
            return

        if not self._available:
            self._available = True
            self._reason = "ok"
            self._seed_levels(book, now_ns)
            self._evict()
            return

        for ch in changes:
            self._apply_change(ch, now_ns)

        # The full snapshot is authoritative for what is CURRENTLY visible. This also
        # handles row shifts / edge visibility without pretending those are cancels/adds.
        self._sync_visible_levels(book, now_ns)
        self._evict()

    def _apply_change(self, ch: LevelChange, now_ns: int) -> None:
        key = (ch.side, ch.price_units)
        level = self._levels.get(key)

        if ch.at_window_edge:
            self._liq_events.append((now_ns, ch.side, 0, 0, 0, 0, 1))
            return

        added = max(0, ch.new_size - ch.old_size)
        removed = max(0, ch.old_size - ch.new_size)
        replenished = 0
        replenish_event = 0

        if added:
            replenished = self._match_recent_hit(ch.side, ch.price_units, added, now_ns)
            replenish_event = int(replenished > 0)

        self._liq_events.append(
            (now_ns, ch.side, added, removed, replenished, replenish_event, 0)
        )

        if level is not None:
            level.visible_additions += added
            level.visible_removals += removed
            if replenished:
                level.replenishment_events += 1
                level.replenished_volume += replenished

    def _seed_levels(self, book: BookSnapshot, now_ns: int) -> None:
        self._levels.clear()
        for side, rows in ((BookSide.BID, book.bids), (BookSide.ASK, book.asks)):
            for price, size in rows:
                self._levels[(side, price)] = _Level(side, price, size, now_ns, size)

    def _sync_visible_levels(self, book: BookSnapshot, now_ns: int) -> None:
        visible: dict[tuple[BookSide, int], int] = {}
        for side, rows in ((BookSide.BID, book.bids), (BookSide.ASK, book.asks)):
            for price, size in rows:
                visible[(side, price)] = size

        for key in tuple(self._levels):
            if key not in visible:
                del self._levels[key]

        for key, size in visible.items():
            level = self._levels.get(key)
            if level is None:
                side, price = key
                self._levels[key] = _Level(side, price, size, now_ns, size)
            else:
                level.size = size
                if size > level.peak_size:
                    level.peak_size = size

    # ---------------------------------------------------------------- trades
    def on_trade(self, trade: ClassifiedTrade, book: BookSnapshot) -> None:
        """Observe known aggressive volume hitting the currently visible opposing best.

        UNKNOWN trades are intentionally not redistributed. A BUY must print exactly at the
        current best ask; a SELL exactly at the current best bid. Deeper sweep attribution is
        a separate C8 layer and is deliberately not guessed here.
        """
        now_ns = trade.recv_mono_ns
        self._advance_clock(now_ns)
        if not self._available or book.state is not BookState.VALID:
            self._evict()
            return
        if not trade.eligible or trade.size <= 0:
            self._evict()
            return

        side: BookSide | None = None
        best: int | None = None
        if trade.aggressor is Aggressor.BUY and book.asks:
            side = BookSide.ASK
            best = book.asks[0][0]
        elif trade.aggressor is Aggressor.SELL and book.bids:
            side = BookSide.BID
            best = book.bids[0][0]

        if side is None or best is None or trade.price_units != best:
            self._evict()
            return

        self._recent_hits.append(_Hit(now_ns, side, best, trade.size))
        self._hit_events.append((now_ns, trade.aggressor, trade.size))
        self._evict()

    def _match_recent_hit(
        self,
        side: BookSide,
        price_units: int,
        displayed_add: int,
        now_ns: int,
    ) -> int:
        self._prune_hits(now_ns)
        remaining_add = displayed_add
        matched = 0
        for hit in self._recent_hits:
            if remaining_add <= 0:
                break
            if hit.side is not side or hit.price_units != price_units or hit.remaining <= 0:
                continue
            use = min(remaining_add, hit.remaining)
            hit.remaining -= use
            remaining_add -= use
            matched += use
        return matched

    # ---------------------------------------------------------------- continuity/time
    def break_book(self, reason: str, now_ns: int | None = None) -> None:
        if now_ns is not None:
            self._advance_clock(now_ns)
        if self._available or self._levels or self._liq_events or self._hit_events:
            self.continuity_epoch += 1
        self._clear(reason)

    def advance(self, now_ns: int) -> None:
        self._advance_clock(now_ns)
        self._evict()

    def _clear(self, reason: str) -> None:
        self._available = False
        self._reason = reason
        self._levels.clear()
        self._liq_events.clear()
        self._hit_events.clear()
        self._recent_hits.clear()

    def _advance_clock(self, now_ns: int) -> None:
        if now_ns > self._now_ns:
            self._now_ns = now_ns

    def _prune_hits(self, now_ns: int) -> None:
        cutoff = now_ns - self._link_ns
        while self._recent_hits and self._recent_hits[0].at_ns < cutoff:
            self._recent_hits.popleft()
        while self._recent_hits and self._recent_hits[0].remaining <= 0:
            self._recent_hits.popleft()

    def _evict(self) -> None:
        cutoff = self._now_ns - 30 * _S
        while self._liq_events and self._liq_events[0][0] < cutoff:
            self._liq_events.popleft()
        while self._hit_events and self._hit_events[0][0] < cutoff:
            self._hit_events.popleft()
        self._prune_hits(self._now_ns)

    # ---------------------------------------------------------------- snapshot
    def _window(self, seconds: int) -> StructureWindow:
        cutoff = self._now_ns - seconds * _S
        ab = rb = aa = ra = repb = repa = reb = rea = edge = 0

        for t, side, added, removed, replenished, rep_event, edge_event in self._liq_events:
            if t < cutoff:
                continue
            edge += edge_event
            if side is BookSide.BID:
                ab += added
                rb += removed
                repb += replenished
                reb += rep_event
            else:
                aa += added
                ra += removed
                repa += replenished
                rea += rep_event

        buy = sell = 0
        for t, agg, volume in self._hit_events:
            if t < cutoff:
                continue
            if agg is Aggressor.BUY:
                buy += volume
            elif agg is Aggressor.SELL:
                sell += volume

        return StructureWindow(
            seconds, ab, rb, aa, ra, repb, repa, reb, rea, buy, sell, edge
        )

    def snapshot(self) -> StructureSnapshot:
        levels = tuple(
            LevelPersistence(
                side=l.side,
                price_units=l.price_units,
                size=l.size,
                age_ns=max(0, self._now_ns - l.first_seen_ns),
                peak_size=l.peak_size,
                visible_additions=l.visible_additions,
                visible_removals=l.visible_removals,
                replenishment_events=l.replenishment_events,
                replenished_volume=l.replenished_volume,
            )
            for l in sorted(
                self._levels.values(),
                key=lambda x: (x.side.value, -x.price_units if x.side is BookSide.BID else x.price_units),
            )
        )
        return StructureSnapshot(
            continuity_epoch=self.continuity_epoch,
            available=self._available,
            reason=self._reason,
            current_levels=levels,
            windows=tuple(self._window(w) for w in WINDOWS_S),
        )

    def token(self) -> tuple:
        return (self.continuity_epoch, self._available, self._reason)

    def fingerprint_state(self) -> tuple:
        levels = tuple(
            (
                k[0], k[1], v.size, v.first_seen_ns, v.peak_size,
                v.visible_additions, v.visible_removals,
                v.replenishment_events, v.replenished_volume,
            )
            for k, v in sorted(self._levels.items(), key=lambda x: (x[0][0].value, x[0][1]))
        )
        hits = tuple((h.at_ns, h.side, h.price_units, h.remaining) for h in self._recent_hits)
        return (
            self.continuity_epoch, self._available, self._reason, self._now_ns, self._link_ns,
            levels, tuple(self._liq_events), tuple(self._hit_events), hits,
        )
