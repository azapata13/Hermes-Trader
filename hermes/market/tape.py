"""Bounded, deterministic Time & Sales tape of classified trades (C4).

* Bounded by COUNT (``max_trades``) and by AGE (``max_age_s``, measured on event
  ``recv_mono_ns`` — never the wall clock). Eviction is oldest-first and deterministic.
* Three DISTINCT totals, all O(1) per trade, all with BUY / SELL / UNKNOWN kept separate
  (UNKNOWN is never folded into BUY or SELL; ``known_delta = buy_volume - sell_volume``):
    - ``retained_window``    : trades currently held by the bounded tape (count/age eviction)
    - ``epoch_cumulative``   : since the current tape epoch began (reset on every continuity break)
    - ``session_cumulative`` : since process start (never evicted, never reset)
* ``epoch`` increments whenever tape continuity may be broken (trades resubscription,
  disconnect, 10197, non-live data). Trades are real prints and are NOT discarded on a new
  epoch; every stored trade carries the epoch it belongs to.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from itertools import islice

from hermes.config import TapeConfig
from hermes.market.classify import Aggressor, ClassMethod, UnknownReason

_S = 1_000_000_000


@dataclass(frozen=True, slots=True)
class ClassifiedTrade:
    instrument_id: int
    seq: int
    generation: int
    tape_epoch: int
    exch_ts_s: int
    recv_mono_ns: int
    recv_wall_ns: int
    price_units: int
    size: int
    exchange: str
    special_conditions: str
    past_limit: bool
    unreported: bool
    eligible: bool
    aggressor: Aggressor
    method: ClassMethod
    confidence: float
    unknown_reason: UnknownReason | None
    quote_bid_units: int | None          # prevailing quote at arrival (context)
    quote_ask_units: int | None
    quote_seq: int | None
    ref_quote_seq: int | None            # quote actually used by the rule
    book_valid: bool                     # depth book state at arrival (context only)
    ref_quote_age_ns: int | None = None  # trade recv - recv of the quote used (calibration data)


@dataclass(slots=True)
class VolumeTotals:
    buy_volume: int = 0
    sell_volume: int = 0
    unknown_volume: int = 0
    buy_trades: int = 0
    sell_trades: int = 0
    unknown_trades: int = 0
    by_method: dict[str, int] = field(default_factory=dict)

    def add(self, t: ClassifiedTrade, sign: int = 1) -> None:
        a = t.aggressor
        if a is Aggressor.BUY:
            self.buy_volume += sign * t.size
            self.buy_trades += sign
        elif a is Aggressor.SELL:
            self.sell_volume += sign * t.size
            self.sell_trades += sign
        else:
            self.unknown_volume += sign * t.size
            self.unknown_trades += sign
        m = t.method.value
        self.by_method[m] = self.by_method.get(m, 0) + sign

    @property
    def known_delta(self) -> int:
        return self.buy_volume - self.sell_volume

    def frozen(self) -> "TotalsSnapshot":
        return TotalsSnapshot(self.buy_volume, self.sell_volume, self.unknown_volume, self.buy_trades,
                              self.sell_trades, self.unknown_trades, self.known_delta,
                              tuple(sorted((k, v) for k, v in self.by_method.items() if v)))


@dataclass(frozen=True, slots=True)
class TotalsSnapshot:
    buy_volume: int
    sell_volume: int
    unknown_volume: int
    buy_trades: int
    sell_trades: int
    unknown_trades: int
    known_delta: int
    by_method: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class TapeSnapshot:
    size: int
    epoch: int
    classifier_epoch: int
    retained_window: TotalsSnapshot      # trades currently on the bounded tape
    epoch_cumulative: TotalsSnapshot     # since the current tape epoch began
    session_cumulative: TotalsSnapshot   # since process start (never evicted or reset)
    latest: tuple[ClassifiedTrade, ...]  # newest first, at most ``snapshot_trades``
    last_aggressor: Aggressor | None
    last_method: ClassMethod | None
    last_confidence: float | None
    context_ok: bool                     # can quote-based classification run right now?
    context_reason: str
    has_quote: bool
    quote_bid_units: int | None
    quote_ask_units: int | None
    tick_direction: Aggressor | None
    evicted_by_count: int
    evicted_by_age: int


class Tape:
    __slots__ = ("_cfg", "_trades", "_max_age_ns", "retained_window", "epoch_cumulative", "session_cumulative",
                 "epoch", "evicted_by_count", "evicted_by_age")

    def __init__(self, cfg: TapeConfig) -> None:
        self._cfg = cfg
        self._trades: deque[ClassifiedTrade] = deque()
        self._max_age_ns = int(cfg.max_age_s * _S)
        self.retained_window = VolumeTotals()
        self.epoch_cumulative = VolumeTotals()
        self.session_cumulative = VolumeTotals()
        self.epoch = 0
        self.evicted_by_count = 0
        self.evicted_by_age = 0

    def __len__(self) -> int:
        return len(self._trades)

    def new_epoch(self) -> None:
        self.epoch += 1
        self.epoch_cumulative = VolumeTotals()

    def append(self, t: ClassifiedTrade) -> None:
        self._trades.append(t)
        self.retained_window.add(t)
        self.epoch_cumulative.add(t)
        self.session_cumulative.add(t)
        while len(self._trades) > self._cfg.max_trades:
            self.retained_window.add(self._trades.popleft(), -1)
            self.evicted_by_count += 1
        self.evict_by_age(t.recv_mono_ns)

    def evict_by_age(self, now_mono_ns: int) -> None:
        horizon = now_mono_ns - self._max_age_ns
        tr = self._trades
        while tr and tr[0].recv_mono_ns < horizon:
            self.retained_window.add(tr.popleft(), -1)
            self.evicted_by_age += 1

    def latest(self, n: int) -> tuple[ClassifiedTrade, ...]:
        return tuple(islice(reversed(self._trades), n))

    def last(self) -> ClassifiedTrade | None:
        return self._trades[-1] if self._trades else None

    def trades(self) -> tuple[ClassifiedTrade, ...]:
        return tuple(self._trades)
