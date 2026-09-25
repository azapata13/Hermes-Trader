"""Deterministic C7 order-flow metrics (measurement only).

No I/O, clocks, threads, network, strategy labels or order logic. All time is
supplied by callers from recorded/live event ``recv_mono_ns``. Rolling windows
are bounded to 30 seconds and UNKNOWN trade volume remains separate.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import lcm

from hermes.market.classify import Aggressor
from hermes.market.orderbook import BookSnapshot, BookState
from hermes.market.tape import ClassifiedTrade

_S = 1_000_000_000
WINDOWS_S = (1, 5, 30)
BOOK_LEVELS = (1, 3, 5, 10)


@dataclass(frozen=True, slots=True)
class RatioMetric:
    requested_levels: int
    bid_levels: int
    ask_levels: int
    bid_total: int
    ask_total: int
    numerator: int
    denominator: int
    available: bool

    @property
    def value(self) -> float | None:
        return None if not self.available or self.denominator == 0 else self.numerator / self.denominator


@dataclass(frozen=True, slots=True)
class BookMetrics:
    available: bool
    reason: str
    continuity_epoch: int
    spread_units: int | None
    mid_x2: int | None
    l1: RatioMetric | None
    l3: RatioMetric | None
    l5: RatioMetric | None
    l10: RatioMetric | None
    weighted: RatioMetric | None
    micro_num: int | None
    micro_den: int | None
    micro_offset_num: int | None
    micro_offset_den: int | None

    @property
    def microprice_units(self) -> float | None:
        return None if self.micro_num is None or not self.micro_den else self.micro_num / self.micro_den

    @property
    def micro_offset_units(self) -> float | None:
        return (None if self.micro_offset_num is None or not self.micro_offset_den
                else self.micro_offset_num / self.micro_offset_den)


@dataclass(frozen=True, slots=True)
class OfiWindow:
    seconds: int
    value: int
    events: int


@dataclass(frozen=True, slots=True)
class TradeFlow:
    seconds: int
    buy_volume: int
    sell_volume: int
    unknown_volume: int
    buy_trades: int
    sell_trades: int
    unknown_trades: int

    @property
    def known_delta(self) -> int:
        return self.buy_volume - self.sell_volume

    @property
    def total_volume(self) -> int:
        return self.buy_volume + self.sell_volume + self.unknown_volume

    @property
    def trade_count(self) -> int:
        return self.buy_trades + self.sell_trades + self.unknown_trades

    @property
    def known_volume_ratio(self) -> float | None:
        total = self.total_volume
        return None if total == 0 else (self.buy_volume + self.sell_volume) / total


@dataclass(frozen=True, slots=True)
class Velocity:
    seconds: int
    trades: int
    contracts: int
    buy_contracts: int
    sell_contracts: int
    depth_updates: int
    bbo_updates: int

    @property
    def trades_per_s(self) -> float:
        return self.trades / self.seconds

    @property
    def contracts_per_s(self) -> float:
        return self.contracts / self.seconds

    @property
    def buy_contracts_per_s(self) -> float:
        return self.buy_contracts / self.seconds

    @property
    def sell_contracts_per_s(self) -> float:
        return self.sell_contracts / self.seconds

    @property
    def depth_updates_per_s(self) -> float:
        return self.depth_updates / self.seconds

    @property
    def bbo_updates_per_s(self) -> float:
        return self.bbo_updates / self.seconds


@dataclass(frozen=True, slots=True)
class PriceMove:
    seconds: int
    midpoint_x2_change: int | None
    last_trade_change: int | None


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    continuity_epoch: int
    book: BookMetrics
    event_ofi: int | None
    ofi: tuple[OfiWindow, ...]
    trade_flow: tuple[TradeFlow, ...]
    velocity: tuple[Velocity, ...]
    price_move: tuple[PriceMove, ...]


def _ratio(bids: tuple[tuple[int, int], ...], asks: tuple[tuple[int, int], ...],
           levels: int) -> RatioMetric:
    bn = min(levels, len(bids))
    an = min(levels, len(asks))
    bt = sum(size for _, size in bids[:bn])
    at = sum(size for _, size in asks[:an])
    den = bt + at
    return RatioMetric(levels, bn, an, bt, at, bt - at, den,
                       bn == levels and an == levels and den > 0)


def _weighted_ratio(bids: tuple[tuple[int, int], ...], asks: tuple[tuple[int, int], ...],
                    levels: int = 10) -> RatioMetric:
    # Exact integer weights equivalent to 1/(level_index+1), avoiding float state.
    scale = 1
    for d in range(1, levels + 1):
        scale = lcm(scale, d)
    bn = min(levels, len(bids))
    an = min(levels, len(asks))
    bt = sum(size * (scale // (i + 1)) for i, (_, size) in enumerate(bids[:bn]))
    at = sum(size * (scale // (i + 1)) for i, (_, size) in enumerate(asks[:an]))
    den = bt + at
    return RatioMetric(levels, bn, an, bt, at, bt - at, den,
                       bn == levels and an == levels and den > 0)


def book_metrics(book: BookSnapshot, continuity_epoch: int) -> BookMetrics:
    if book.state is not BookState.VALID:
        return BookMetrics(False, f"book_{book.state.value}", continuity_epoch,
                           None, None, None, None, None, None, None, None, None, None, None)
    if not book.bids or not book.asks:
        return BookMetrics(False, "book_empty_side", continuity_epoch,
                           None, None, None, None, None, None, None, None, None, None, None)

    bid_p, bid_q = book.bids[0]
    ask_p, ask_q = book.asks[0]
    den = bid_q + ask_q
    micro_num = ask_q * bid_p + bid_q * ask_p if den > 0 else None
    micro_den = den if den > 0 else None
    mid_x2 = bid_p + ask_p
    offset_num = 2 * micro_num - mid_x2 * den if micro_num is not None else None
    offset_den = 2 * den if den > 0 else None
    ratios = {n: _ratio(book.bids, book.asks, n) for n in BOOK_LEVELS}
    return BookMetrics(
        True, "ok", continuity_epoch, ask_p - bid_p, mid_x2,
        ratios[1], ratios[3], ratios[5], ratios[10],
        _weighted_ratio(book.bids, book.asks, 10),
        micro_num, micro_den, offset_num, offset_den,
    )


def _ofi(prev: tuple[int, int, int, int], cur: tuple[int, int, int, int]) -> int:
    """Cont-style L1 OFI from consecutive best bid/ask states."""
    pb0, qb0, pa0, qa0 = prev
    pb1, qb1, pa1, qa1 = cur
    e = 0
    if pb1 >= pb0:
        e += qb1
    if pb1 <= pb0:
        e -= qb0
    if pa1 <= pa0:
        e -= qa1
    if pa1 >= pa0:
        e += qa0
    return e


class MetricsEngine:
    """Per-instrument deterministic C7 metric state."""

    __slots__ = (
        "instrument_id", "continuity_epoch", "_book_valid", "_book_reason", "_book_snapshot",
        "_prev_l1", "_event_ofi", "_ofi_events", "_trades", "_depth_events", "_bbo_events",
        "_midpoints", "_last_prices", "_now_ns",
    )

    def __init__(self, instrument_id: int) -> None:
        self.instrument_id = instrument_id
        self.continuity_epoch = 0
        self._book_valid = False
        self._book_reason = "not_observed"
        self._book_snapshot: BookSnapshot | None = None
        self._prev_l1: tuple[int, int, int, int] | None = None
        self._event_ofi: int | None = None
        self._ofi_events: deque[tuple[int, int]] = deque()
        self._trades: deque[tuple[int, int, Aggressor, int]] = deque()
        self._depth_events: deque[int] = deque()
        self._bbo_events: deque[int] = deque()
        self._midpoints: deque[tuple[int, int]] = deque()
        self._last_prices: deque[tuple[int, int]] = deque()
        self._now_ns = 0

    def observe_book(self, book: BookSnapshot, now_ns: int, *, depth_event: bool = False) -> None:
        self._advance_clock(now_ns)
        self._book_snapshot = book
        if depth_event:
            self._depth_events.append(now_ns)

        if book.state is not BookState.VALID or not book.bids or not book.asks:
            if self._book_valid:
                self._break_book(f"book_{book.state.value}")
            self._book_valid = False
            self._book_reason = f"book_{book.state.value}"
            self._evict()
            return

        bid_p, bid_q = book.bids[0]
        ask_p, ask_q = book.asks[0]
        cur = (bid_p, bid_q, ask_p, ask_q)
        mid_x2 = bid_p + ask_p

        if not self._book_valid:
            self._book_valid = True
            self._book_reason = "ok"
            self._prev_l1 = cur
        elif depth_event and self._prev_l1 is not None:
            val = _ofi(self._prev_l1, cur)
            self._event_ofi = val
            self._ofi_events.append((now_ns, val))
            self._prev_l1 = cur
        else:
            self._prev_l1 = cur

        if not self._midpoints or self._midpoints[-1][1] != mid_x2:
            self._midpoints.append((now_ns, mid_x2))
        self._evict()

    def on_bbo(self, now_ns: int) -> None:
        self._advance_clock(now_ns)
        self._bbo_events.append(now_ns)
        self._evict()

    def on_trade(self, trade: ClassifiedTrade) -> None:
        now_ns = trade.recv_mono_ns
        self._advance_clock(now_ns)
        self._trades.append((now_ns, trade.size, trade.aggressor, trade.price_units))
        if not self._last_prices or self._last_prices[-1][1] != trade.price_units:
            self._last_prices.append((now_ns, trade.price_units))
        self._evict()

    def advance(self, now_ns: int) -> None:
        self._advance_clock(now_ns)
        self._evict()

    def break_book(self, reason: str, now_ns: int | None = None) -> None:
        if now_ns is not None:
            self._advance_clock(now_ns)
        self.continuity_epoch += 1
        self._break_book(reason)

    def break_trade(self, reason: str, now_ns: int | None = None) -> None:
        if now_ns is not None:
            self._advance_clock(now_ns)
        self.continuity_epoch += 1
        self._trades.clear()
        self._last_prices.clear()

    def break_all(self, reason: str, now_ns: int | None = None) -> None:
        if now_ns is not None:
            self._advance_clock(now_ns)
        self.continuity_epoch += 1
        self._break_book(reason)
        self._trades.clear()
        self._depth_events.clear()
        self._bbo_events.clear()
        self._last_prices.clear()

    def _break_book(self, reason: str) -> None:
        self._book_valid = False
        self._book_reason = reason
        self._book_snapshot = None
        self._prev_l1 = None
        self._event_ofi = None
        self._ofi_events.clear()
        self._midpoints.clear()

    def _advance_clock(self, now_ns: int) -> None:
        if now_ns > self._now_ns:
            self._now_ns = now_ns

    def _evict(self) -> None:
        cutoff = self._now_ns - 30 * _S
        while self._ofi_events and self._ofi_events[0][0] < cutoff:
            self._ofi_events.popleft()
        while self._trades and self._trades[0][0] < cutoff:
            self._trades.popleft()
        while self._depth_events and self._depth_events[0] < cutoff:
            self._depth_events.popleft()
        while self._bbo_events and self._bbo_events[0] < cutoff:
            self._bbo_events.popleft()
        for q in (self._midpoints, self._last_prices):
            while len(q) > 1 and q[1][0] <= cutoff:
                q.popleft()

    @staticmethod
    def _since(window_s: int, now_ns: int) -> int:
        return now_ns - window_s * _S

    def _ofi_window(self, seconds: int) -> OfiWindow:
        cutoff = self._since(seconds, self._now_ns)
        vals = [v for t, v in self._ofi_events if t >= cutoff]
        return OfiWindow(seconds, sum(vals), len(vals))

    def _trade_window(self, seconds: int) -> TradeFlow:
        cutoff = self._since(seconds, self._now_ns)
        bv = sv = uv = bt = st = ut = 0
        for t, size, side, _price in self._trades:
            if t < cutoff:
                continue
            if side is Aggressor.BUY:
                bv += size
                bt += 1
            elif side is Aggressor.SELL:
                sv += size
                st += 1
            else:
                uv += size
                ut += 1
        return TradeFlow(seconds, bv, sv, uv, bt, st, ut)

    def _velocity_window(self, seconds: int) -> Velocity:
        cutoff = self._since(seconds, self._now_ns)
        trades = [x for x in self._trades if x[0] >= cutoff]
        return Velocity(
            seconds=seconds,
            trades=len(trades),
            contracts=sum(x[1] for x in trades),
            buy_contracts=sum(x[1] for x in trades if x[2] is Aggressor.BUY),
            sell_contracts=sum(x[1] for x in trades if x[2] is Aggressor.SELL),
            depth_updates=sum(1 for t in self._depth_events if t >= cutoff),
            bbo_updates=sum(1 for t in self._bbo_events if t >= cutoff),
        )

    @staticmethod
    def _price_change(points: deque[tuple[int, int]], now_ns: int, seconds: int) -> int | None:
        if not points:
            return None
        target = now_ns - seconds * _S
        anchor = None
        for t, value in points:
            if t <= target:
                anchor = value
            else:
                break
        if anchor is None:
            return None
        return points[-1][1] - anchor

    def snapshot(self) -> MetricsSnapshot:
        book = (book_metrics(self._book_snapshot, self.continuity_epoch)
                if self._book_snapshot is not None
                else BookMetrics(False, self._book_reason, self.continuity_epoch,
                                 None, None, None, None, None, None, None, None, None, None, None))
        return MetricsSnapshot(
            continuity_epoch=self.continuity_epoch,
            book=book,
            event_ofi=self._event_ofi,
            ofi=tuple(self._ofi_window(w) for w in WINDOWS_S),
            trade_flow=tuple(self._trade_window(w) for w in WINDOWS_S),
            velocity=tuple(self._velocity_window(w) for w in WINDOWS_S),
            price_move=tuple(PriceMove(
                w,
                self._price_change(self._midpoints, self._now_ns, w),
                self._price_change(self._last_prices, self._now_ns, w),
            ) for w in WINDOWS_S),
        )

    def token(self) -> tuple:
        """Cheap state token used for immediate snapshot publication on C7 continuity/quality changes."""
        return (
            self.continuity_epoch,
            self._book_valid,
            self._book_reason,
        )

    def fingerprint_state(self) -> tuple:
        """Exact bounded state needed for deterministic future rolling-window behavior."""
        return (
            self.continuity_epoch, self._book_valid, self._book_reason, self._book_snapshot,
            self._prev_l1, self._event_ofi, tuple(self._ofi_events), tuple(self._trades),
            tuple(self._depth_events), tuple(self._bbo_events), tuple(self._midpoints),
            tuple(self._last_prices), self._now_ns,
        )
