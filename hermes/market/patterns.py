"""C8c deterministic sweep and follow-through measurements.

Measurement-only: no LONG/SHORT signal, score, or order logic.
"""

from __future__ import annotations
from collections import deque
from dataclasses import dataclass
from enum import Enum

from hermes.market.classify import Aggressor
from hermes.market.orderbook import BookSnapshot, BookState
from hermes.market.tape import ClassifiedTrade

_S = 1_000_000_000
WINDOWS_S = (1, 5, 30)


class FollowResult(Enum):
    FOLLOW_THROUGH = "follow_through"
    NO_FOLLOW_THROUGH = "no_follow_through"


@dataclass(frozen=True, slots=True)
class SweepEvent:
    start_ns: int
    end_ns: int
    aggressor: Aggressor
    start_price_units: int
    end_price_units: int
    levels: int
    volume: int
    trades: int

    @property
    def span_units(self) -> int:
        return abs(self.end_price_units - self.start_price_units)


@dataclass(frozen=True, slots=True)
class FollowEvent:
    trade_ns: int
    resolved_ns: int
    aggressor: Aggressor
    trade_price_units: int
    volume: int
    start_mid_x2: int
    end_mid_x2: int
    result: FollowResult

    @property
    def favorable_mid_x2(self) -> int:
        d = self.end_mid_x2 - self.start_mid_x2
        return d if self.aggressor is Aggressor.BUY else -d


@dataclass(frozen=True, slots=True)
class PatternWindow:
    seconds: int
    buy_sweeps: int
    sell_sweeps: int
    buy_sweep_volume: int
    sell_sweep_volume: int
    max_sweep_levels: int
    buy_follow_volume: int
    sell_follow_volume: int
    buy_no_follow_volume: int
    sell_no_follow_volume: int
    follow_events: int
    no_follow_events: int


@dataclass(frozen=True, slots=True)
class PatternSnapshot:
    continuity_epoch: int
    available: bool
    reason: str
    latest_sweeps: tuple[SweepEvent, ...]
    latest_follow: tuple[FollowEvent, ...]
    windows: tuple[PatternWindow, ...]


@dataclass(slots=True)
class _ActiveSweep:
    aggressor: Aggressor
    start_ns: int
    last_ns: int
    start_price: int
    last_price: int
    prices: set[int]
    volume: int
    trades: int


@dataclass(slots=True)
class _PendingFollow:
    trade_ns: int
    deadline_ns: int
    aggressor: Aggressor
    trade_price_units: int
    volume: int
    start_mid_x2: int


class PatternEngine:
    __slots__ = (
        "instrument_id", "continuity_epoch", "_available", "_reason", "_now_ns",
        "_mid_x2", "_sweep_gap_ns", "_follow_horizon_ns", "_active",
        "_sweeps", "_pending", "_follow",
    )

    def __init__(self, instrument_id: int, *, sweep_gap_ms: int = 250, follow_horizon_ms: int = 500) -> None:
        if sweep_gap_ms < 0 or follow_horizon_ms < 0:
            raise ValueError("windows must be >= 0")
        self.instrument_id = instrument_id
        self.continuity_epoch = 0
        self._available = False
        self._reason = "not_observed"
        self._now_ns = 0
        self._mid_x2 = None
        self._sweep_gap_ns = sweep_gap_ms * 1_000_000
        self._follow_horizon_ns = follow_horizon_ms * 1_000_000
        self._active = None
        self._sweeps = deque()
        self._pending = deque()
        self._follow = deque()

    def observe_book(self, book: BookSnapshot, now_ns: int) -> None:
        self._advance(now_ns)
        if book.state is not BookState.VALID or not book.bids or not book.asks:
            reason = f"book_{book.state.value}"
            if self._available:
                self.continuity_epoch += 1
                self._clear(reason)
            else:
                self._reason = reason
            self._evict()
            return
        self._available = True
        self._reason = "ok"
        self._mid_x2 = book.bids[0][0] + book.asks[0][0]
        self._finalize_if_timed_out(now_ns)
        self._resolve_pending(now_ns)
        self._evict()

    def on_trade(self, trade: ClassifiedTrade, book: BookSnapshot) -> None:
        now_ns = trade.recv_mono_ns
        self._advance(now_ns)
        if not self._available or book.state is not BookState.VALID or not book.bids or not book.asks:
            self._evict()
            return
        if not trade.eligible or trade.size <= 0 or trade.aggressor is Aggressor.UNKNOWN:
            self._finalize_active()
            self._evict()
            return

        agg, price = trade.aggressor, trade.price_units
        a = self._active
        monotonic = False
        if a is not None and a.aggressor is agg and now_ns - a.last_ns <= self._sweep_gap_ns:
            monotonic = price >= a.last_price if agg is Aggressor.BUY else price <= a.last_price

        if a is None or not monotonic:
            self._finalize_active()
            self._active = _ActiveSweep(agg, now_ns, now_ns, price, price, {price}, trade.size, 1)
        else:
            a.last_ns = now_ns
            a.last_price = price
            a.prices.add(price)
            a.volume += trade.size
            a.trades += 1

        mid_x2 = book.bids[0][0] + book.asks[0][0]
        self._pending.append(_PendingFollow(
            now_ns, now_ns + self._follow_horizon_ns, agg, price, trade.size, mid_x2
        ))
        self._evict()

    def advance(self, now_ns: int) -> None:
        self._advance(now_ns)
        self._finalize_if_timed_out(now_ns)
        self._resolve_pending(now_ns)
        self._evict()

    def break_book(self, reason: str, now_ns: int | None = None) -> None:
        if now_ns is not None:
            self._advance(now_ns)
        if self._available or self._active is not None or self._pending or self._sweeps or self._follow:
            self.continuity_epoch += 1
        self._clear(reason)

    def break_trades(self, reason: str, now_ns: int | None = None) -> None:
        if now_ns is not None:
            self._advance(now_ns)
        if self._available or self._active is not None or self._pending:
            self.continuity_epoch += 1
        self._active = None
        self._pending.clear()
        self._sweeps.clear()
        self._follow.clear()
        self._reason = reason if not self._available else "ok"

    def _clear(self, reason: str) -> None:
        self._available = False
        self._reason = reason
        self._mid_x2 = None
        self._active = None
        self._sweeps.clear()
        self._pending.clear()
        self._follow.clear()

    def _advance(self, now_ns: int) -> None:
        if now_ns > self._now_ns:
            self._now_ns = now_ns

    def _finalize_if_timed_out(self, now_ns: int) -> None:
        if self._active is not None and now_ns - self._active.last_ns > self._sweep_gap_ns:
            self._finalize_active()

    def _finalize_active(self) -> None:
        a = self._active
        self._active = None
        if a is None or len(a.prices) < 2 or a.trades < 2:
            return
        self._sweeps.append(SweepEvent(
            a.start_ns, a.last_ns, a.aggressor, a.start_price, a.last_price,
            len(a.prices), a.volume, a.trades
        ))

    def _resolve_pending(self, now_ns: int) -> None:
        if not self._available or self._mid_x2 is None:
            return
        while self._pending and self._pending[0].deadline_ns <= now_ns:
            p = self._pending.popleft()
            d = self._mid_x2 - p.start_mid_x2
            favorable = d > 0 if p.aggressor is Aggressor.BUY else d < 0
            self._follow.append(FollowEvent(
                p.trade_ns, now_ns, p.aggressor, p.trade_price_units, p.volume,
                p.start_mid_x2, self._mid_x2,
                FollowResult.FOLLOW_THROUGH if favorable else FollowResult.NO_FOLLOW_THROUGH
            ))

    def _evict(self) -> None:
        cutoff = self._now_ns - 30 * _S
        while self._sweeps and self._sweeps[0].end_ns < cutoff:
            self._sweeps.popleft()
        while self._follow and self._follow[0].resolved_ns < cutoff:
            self._follow.popleft()

    def _window(self, seconds: int) -> PatternWindow:
        cutoff = self._now_ns - seconds * _S
        bs = ss = bsv = ssv = max_levels = 0
        for e in self._sweeps:
            if e.end_ns < cutoff:
                continue
            max_levels = max(max_levels, e.levels)
            if e.aggressor is Aggressor.BUY:
                bs += 1
                bsv += e.volume
            else:
                ss += 1
                ssv += e.volume

        bfv = sfv = bnfv = snfv = fe = nfe = 0
        for e in self._follow:
            if e.resolved_ns < cutoff:
                continue
            if e.result is FollowResult.FOLLOW_THROUGH:
                fe += 1
                if e.aggressor is Aggressor.BUY:
                    bfv += e.volume
                else:
                    sfv += e.volume
            else:
                nfe += 1
                if e.aggressor is Aggressor.BUY:
                    bnfv += e.volume
                else:
                    snfv += e.volume

        return PatternWindow(seconds, bs, ss, bsv, ssv, max_levels, bfv, sfv, bnfv, snfv, fe, nfe)

    def snapshot(self) -> PatternSnapshot:
        return PatternSnapshot(
            self.continuity_epoch, self._available, self._reason,
            tuple(reversed(tuple(self._sweeps)[-5:])),
            tuple(reversed(tuple(self._follow)[-10:])),
            tuple(self._window(w) for w in WINDOWS_S),
        )

    def token(self) -> tuple:
        return (self.continuity_epoch, self._available, self._reason)

    def fingerprint_state(self) -> tuple:
        a = self._active
        active = None if a is None else (
            a.aggressor, a.start_ns, a.last_ns, a.start_price, a.last_price,
            tuple(sorted(a.prices)), a.volume, a.trades
        )
        pending = tuple(
            (p.trade_ns, p.deadline_ns, p.aggressor, p.trade_price_units, p.volume, p.start_mid_x2)
            for p in self._pending
        )
        return (
            self.continuity_epoch, self._available, self._reason, self._now_ns, self._mid_x2,
            self._sweep_gap_ns, self._follow_horizon_ns, active,
            tuple(self._sweeps), pending, tuple(self._follow)
        )
