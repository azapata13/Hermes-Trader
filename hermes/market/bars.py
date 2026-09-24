"""Multi-timeframe bars (C5): canonical 30 s bars from classified trades, 1 m / 5 m aggregated.

Architecture
------------
* **30 s is canonical** and built from ``ClassifiedTrade`` data. 1 m bars are aggregated ONLY from
  completed 30 s bars and 5 m bars ONLY from completed 1 m bars, with exact integer consistency
  (OHLC, volume, trades, buy/sell/unknown volume, known_delta, VWAP numerator, seq range, flags).
* **Time basis.** Membership uses the trade's exchange timestamp (1 s resolution; the local
  receive wall time only if the exchange time is missing). Bars are aligned to the UTC epoch:
  ``start = ts - ts % 30``; a print stamped exactly ``:30`` belongs to the NEXT bar.
* **Closing.** The engine feeds an event-time watermark (max recorded ``recv_wall_ns`` of events;
  no clock is read here). A bar is FINAL once the watermark passes ``end + close_grace_ms``
  (500 ms, provisional). Finalization happens on clock ticks / control / connection / error /
  heartbeat / trade events — deterministic and replayable.
* **Late prints** (exchange time inside an already-final bar) never rewrite it and are never moved
  into a later bar: they are counted (trades, volume) and flag the bar forming at that moment with
  ``LATE_DATA_OBSERVED``.
* **Empty intervals** become flat zero-volume ``EMPTY`` bars at the last known price, only inside an
  active trading session (per the contract's ``tradingHours``) and only after the first known price.
  Closures (weekend, CME daily maintenance break) and an unknown calendar never produce bars.
* **Quality flags** are sticky: once set on a bar they are never cleared (a reconnect does not
  clean a bar) and they are ORed into the 1 m / 5 m aggregates.
* ``BarEligibilityPolicy`` is independent of the classifier's eligibility: a print may be
  classifier-ineligible (aggressor UNKNOWN) yet bar-eligible, or be preserved on the tape but
  excluded from bars (counted per reason). Excluded prints never touch OHLC or volume.
* Bounded memory: completed bars per timeframe in FIFO deques (``history_*``).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum, IntFlag
from itertools import islice

from hermes.config import BarsConfig
from hermes.market.classify import Aggressor
from hermes.market.sessions import SessionCalendar

_S = 1_000_000_000
_MS = 1_000_000
BASE_S = 30
TIMEFRAMES = (30, 60, 300)
_MAX_MARKS = 256


class BarFlag(IntFlag):
    NONE = 0
    EMPTY = 1                     # no bar-eligible prints: flat at the last known price
    FORMING = 2                   # not final yet (snapshots only)
    PARTIAL = 4                   # the interval was not fully observed (startup / missing children)
    DATA_GAP = 8                  # prints may be missing (continuity break / outage overlapped the bar)
    MARKET_DATA_INVALID = 16      # trades data unusable during part of the bar (not live, 10197, farm, stream error)
    CONNECTION_INTERRUPTION = 32  # API connection lost/closed/1100/1101 during the bar
    LATE_DATA_OBSERVED = 64       # a late print (for an already-final bar) arrived while this bar was forming
    SESSION_BOUNDARY = 128        # a trading-session open/close touches the bar (or children span dates)


# Plain-int views (IntFlag arithmetic is slow in CPython; bars carry a BarFlag, internals use ints).
_EMPTY, _FORMING, _PARTIAL = int(BarFlag.EMPTY), int(BarFlag.FORMING), int(BarFlag.PARTIAL)
_DATA_GAP, _LATE, _BOUNDARY = int(BarFlag.DATA_GAP), int(BarFlag.LATE_DATA_OBSERVED), int(BarFlag.SESSION_BOUNDARY)
_INHERIT_MASK = ~(_EMPTY | _FORMING)


class BarExclusion(str, Enum):
    NON_POSITIVE_SIZE = "non_positive_size"
    PAST_LIMIT = "past_limit"
    UNREPORTED = "unreported"
    SPECIAL_CONDITION = "special_condition"


class TradeDisposition(str, Enum):
    INCLUDED = "included"         # used for OHLC / volume / flow
    EXCLUDED = "excluded"         # preserved on the tape, excluded from bars (counted)
    LATE = "late"                 # its bar was already final (counted, never rewrites)


class BarEligibilityPolicy:
    """Which prints build bars. Conservative defaults where IBKR semantics are uncertain:
    pastLimit, unreported and non-allowlisted special conditions are EXCLUDED (and counted)."""

    __slots__ = ("_cfg", "_allowed")

    def __init__(self, cfg: BarsConfig) -> None:
        self._cfg = cfg
        self._allowed = frozenset(c.strip() for c in cfg.allowed_special_conditions.split(",") if c.strip())

    def evaluate(self, size: int, past_limit: bool, unreported: bool, special_conditions: str) -> BarExclusion | None:
        if size <= 0:
            return BarExclusion.NON_POSITIVE_SIZE
        if past_limit and not self._cfg.include_past_limit:
            return BarExclusion.PAST_LIMIT
        if unreported and not self._cfg.include_unreported:
            return BarExclusion.UNREPORTED
        conds = special_conditions.strip()
        if conds and not all(c.strip() in self._allowed for c in conds.split(",") if c.strip()):
            return BarExclusion.SPECIAL_CONDITION
        return None


@dataclass(frozen=True, slots=True)
class Bar:
    instrument_id: int
    timeframe_s: int
    start_s: int                  # UTC epoch seconds (inclusive)
    end_s: int                    # exclusive
    open: int                     # integer price units
    high: int
    low: int
    close: int
    volume: int
    trades: int
    buy_volume: int
    sell_volume: int
    unknown_volume: int           # never folded into buy or sell
    known_delta: int              # buy_volume - sell_volume (UNKNOWN excluded)
    vwap_num: int                 # sum(price_units * size): exact integer VWAP numerator
    first_seq: int | None
    last_seq: int | None
    excluded_trades: int          # bar-ineligible prints stamped in this bar (not in OHLC/volume)
    excluded_volume: int
    flags: BarFlag
    trading_date: str             # exchange trading date / session id ("" if calendar unknown)

    @property
    def vwap(self) -> float | None:
        return self.vwap_num / self.volume if self.volume else None


class _Acc:
    """Mutable accumulator for a 30 s bar or an aggregate (same attribute names as ``Bar``)."""

    __slots__ = ("start_s", "open", "high", "low", "close", "volume", "trades", "buy_volume", "sell_volume",
                 "unknown_volume", "vwap_num", "first_seq", "last_seq", "excluded_trades", "excluded_volume",
                 "flags", "children", "flat")

    def __init__(self, start_s: int) -> None:
        self.start_s = start_s
        self.open = self.high = self.low = self.close = None
        self.volume = self.trades = self.buy_volume = self.sell_volume = self.unknown_volume = 0
        self.vwap_num = 0
        self.first_seq = self.last_seq = None
        self.excluded_trades = self.excluded_volume = 0
        self.flags = 0
        self.children = 0
        self.flat = None              # aggregates: price of the first child (all-EMPTY case)

    def add_trade(self, price: int, size: int, seq: int, aggressor: Aggressor) -> None:
        if self.open is None:
            self.open = self.high = self.low = price
            self.first_seq = seq
        else:
            if price > self.high:
                self.high = price
            if price < self.low:
                self.low = price
        self.close = price
        self.last_seq = seq
        self.volume += size
        self.trades += 1
        self.vwap_num += price * size
        if aggressor is Aggressor.BUY:
            self.buy_volume += size
        elif aggressor is Aggressor.SELL:
            self.sell_volume += size
        else:
            self.unknown_volume += size

    def merge(self, b) -> None:
        """Fold a completed child (``Bar``) or a partial accumulator into this aggregate."""
        self.children += 1
        if self.flat is None:
            self.flat = b.open
        self.flags |= int(b.flags) & _INHERIT_MASK
        self.excluded_trades += b.excluded_trades
        self.excluded_volume += b.excluded_volume
        if not b.trades:
            return                   # EMPTY children never contribute OHLC (exact vs direct computation)
        if self.open is None:
            self.open, self.high, self.low = b.open, b.high, b.low
            self.first_seq = b.first_seq
        else:
            if b.high > self.high:
                self.high = b.high
            if b.low < self.low:
                self.low = b.low
        self.close = b.close
        self.last_seq = b.last_seq
        self.volume += b.volume
        self.trades += b.trades
        self.buy_volume += b.buy_volume
        self.sell_volume += b.sell_volume
        self.unknown_volume += b.unknown_volume
        self.vwap_num += b.vwap_num

    def freeze(self, iid: int, tf: int, extra: int, trading_date: str, fallback: int | None) -> Bar | None:
        flags = self.flags | extra
        if self.trades:
            o, h, lo, c = self.open, self.high, self.low, self.close
        else:
            p = self.flat if self.flat is not None else fallback
            if p is None:
                return None
            o = h = lo = c = p
            flags |= _EMPTY
        return Bar(iid, tf, self.start_s, self.start_s + tf, o, h, lo, c, self.volume, self.trades,  # type: ignore[arg-type]
                   self.buy_volume, self.sell_volume, self.unknown_volume, self.buy_volume - self.sell_volume,
                   self.vwap_num, self.first_seq, self.last_seq, self.excluded_trades, self.excluded_volume,
                   BarFlag(flags), trading_date)


class _Agg:
    """Aggregates completed child bars of ``child_tf`` into ``tf`` bars."""

    __slots__ = ("tf", "child_tf", "iid", "acc", "td", "mixed")

    def __init__(self, tf: int, child_tf: int, iid: int) -> None:
        self.tf, self.child_tf, self.iid = tf, child_tf, iid
        self.acc: _Acc | None = None
        self.td = ""
        self.mixed = False

    def add(self, bar: Bar) -> list[Bar]:
        out: list[Bar] = []
        p = bar.start_s - bar.start_s % self.tf
        if self.acc is not None and self.acc.start_s != p:
            out.append(self._close())                 # defensive; advance() normally closes first
        if self.acc is None:
            self.acc = _Acc(p)
            self.td, self.mixed = bar.trading_date, False
        elif bar.trading_date != self.td:
            self.mixed = True
        self.acc.merge(bar)
        if bar.end_s >= p + self.tf:
            out.append(self._close())
        return out

    def advance(self, finalized_end_s: int) -> Bar | None:
        if self.acc is not None and finalized_end_s >= self.acc.start_s + self.tf:
            return self._close()
        return None

    def _close(self) -> Bar:
        acc = self.acc
        self.acc = None
        extra = 0
        if acc.children < self.tf // self.child_tf:  # type: ignore[union-attr]
            extra |= _PARTIAL
        if self.mixed:
            extra |= _BOUNDARY
        return acc.freeze(self.iid, self.tf, extra, self.td, None)  # type: ignore[union-attr,return-value]


@dataclass(frozen=True, slots=True)
class BarsSnapshot:
    forming_30s: Bar | None       # FORMING flag set; never final
    forming_1m: Bar | None
    forming_5m: Bar | None
    latest_30s: tuple[Bar, ...]   # completed, newest first
    latest_1m: tuple[Bar, ...]
    latest_5m: tuple[Bar, ...]
    completed_30s: int
    completed_1m: int
    completed_5m: int
    finalized_through_s: int | None
    late_trades: int
    late_volume: int
    excluded_trades: int
    excluded_volume: int
    excluded_by_reason: tuple[tuple[str, int], ...]
    empty_bars: int               # completed 30 s EMPTY bars
    gap_bars: int                 # completed 30 s bars flagged DATA_GAP
    active_flags: BarFlag         # quality condition active NOW (0 == bar data currently trustworthy)
    close_grace_ms: int


class BarEngine:
    """Per-instrument bar state. Single writer (the MarketEngine); no clock, no I/O."""

    def __init__(self, cfg: BarsConfig, instrument_id: int) -> None:
        self.cfg = cfg
        self.instrument_id = instrument_id
        self.policy = BarEligibilityPolicy(cfg)
        self.calendar: SessionCalendar | None = None
        self._grace_ns = cfg.close_grace_ms * _MS
        self.open: dict[int, _Acc] = {}
        self.finalized_end_s: int | None = None
        self.last_price: int | None = None
        self.history: dict[int, deque[Bar]] = {30: deque(maxlen=cfg.history_30s), 60: deque(maxlen=cfg.history_1m),
                                               300: deque(maxlen=cfg.history_5m)}
        self.completed = {30: 0, 60: 0, 300: 0}
        self._agg60 = _Agg(60, 30, instrument_id)
        self._agg300 = _Agg(300, 60, instrument_id)
        self._next_agg = {30: self._agg60, 60: self._agg300}
        self.obs_start_ns: int | None = None
        self.wm_ns = 0
        self.next_due_ns: int | None = None
        self.armed = False                      # quality tracking starts at the first trades subscription
        self.cond_flags = 0                     # int view of the active BarFlag condition
        self.cond_since_ns = 0
        self.marks: list[tuple[int, int, int]] = []
        self.late_trades = 0
        self.late_volume = 0
        self.excluded_trades = 0
        self.excluded_volume = 0
        self.excluded_by_reason: dict[str, int] = {}
        self.empty_bars = 0
        self.gap_bars = 0
        self.dropped_no_price = 0
        self._ver = 0                           # bumped on every visible change (snapshot cache key)
        self._snap: tuple[int, int, BarsSnapshot] | None = None

    def set_calendar(self, cal: SessionCalendar) -> None:
        self.calendar = cal
        self._ver += 1

    # ------------------------------------------------------------------ quality marks
    def set_condition(self, flags: int, now_ns: int) -> None:
        flags = int(flags)
        if flags == self.cond_flags:
            return
        self._ver += 1
        if self.cond_flags:
            self._add_mark(self.cond_since_ns, now_ns, self.cond_flags)
        self.cond_flags = flags
        self.cond_since_ns = now_ns

    def mark(self, now_ns: int, flags: int) -> None:
        self._add_mark(now_ns, now_ns, int(flags))

    def _add_mark(self, a: int, b: int, flags: int) -> None:
        self._ver += 1
        m = self.marks
        m.append((a, b, flags))
        if len(m) > _MAX_MARKS:                 # bounded: coalesce (only ever widens flags)
            f = 0
            for x in m:
                f |= x[2]
            self.marks = [(min(x[0] for x in m), max(x[1] for x in m), f)]

    def _flags_for(self, s: int) -> int:
        lo, hi = s * _S, (s + BASE_S) * _S
        f = 0
        if self.cond_flags and self.cond_since_ns < hi:
            f |= self.cond_flags
        for a, b, fl in self.marks:
            if a < hi and b >= lo:
                f |= fl
        return f

    # ------------------------------------------------------------------ trades
    def on_trade(self, ts_s: int, price: int, size: int, seq: int, aggressor: Aggressor,
                 exclusion: BarExclusion | None) -> TradeDisposition:
        self._ver += 1
        slot = ts_s - ts_s % BASE_S
        fe = self.finalized_end_s
        if fe is not None and slot < fe:
            self.late_trades += 1
            self.late_volume += max(size, 0)
            self._add_mark(self.wm_ns, self.wm_ns, _LATE)
            return TradeDisposition.LATE
        acc = self.open.get(slot)
        if acc is None:
            acc = self.open[slot] = _Acc(slot)
            due = (slot + BASE_S) * _S + self._grace_ns
            if fe is None and (self.next_due_ns is None or due < self.next_due_ns):
                self.next_due_ns = due
        if exclusion is not None:
            acc.excluded_trades += 1
            acc.excluded_volume += max(size, 0)
            self.excluded_trades += 1
            self.excluded_volume += max(size, 0)
            self.excluded_by_reason[exclusion.value] = self.excluded_by_reason.get(exclusion.value, 0) + 1
            return TradeDisposition.EXCLUDED
        acc.add_trade(price, size, seq, aggressor)
        return TradeDisposition.INCLUDED

    # ------------------------------------------------------------------ closing
    def _session_active(self, s: int) -> bool:
        cal = self.calendar
        return cal is not None and cal.valid and cal.trading_at(s) is not None

    def advance(self, wm_ns: int) -> None:
        """Event-time watermark (monotonic). Finalizes every bar whose end + grace has passed."""
        self.wm_ns = wm_ns
        if self.obs_start_ns is None:
            self.obs_start_ns = wm_ns
        nd = self.next_due_ns
        if nd is None or wm_ns < nd:
            return
        grace = self._grace_ns
        self._ver += 1
        while True:
            fe = self.finalized_end_s
            if fe is None:
                if not self.open:
                    break
                s = min(self.open)
            else:
                s = fe
            if (s + BASE_S) * _S + grace > wm_ns:
                break
            acc = self.open.pop(s, None)
            if acc is not None:
                self._finalize(acc, s)
                new_fe = s + BASE_S
            elif self.last_price is not None and self._session_active(s):
                self._finalize(_Acc(s), s)                       # EMPTY bar inside an active session
                new_fe = s + BASE_S
            else:                                                # closed / unknown / no price: skip ahead
                closable = ((wm_ns - grace) // _S) // BASE_S * BASE_S
                target = closable
                if self.open:
                    target = min(target, min(self.open))
                cal = self.calendar
                if self.last_price is not None and cal is not None and cal.valid:
                    nxt = cal.next_trading_start(s)
                    if nxt is not None:
                        target = min(target, nxt - nxt % BASE_S)
                new_fe = max(target, s + BASE_S)
            self.finalized_end_s = new_fe
            for agg in (self._agg60, self._agg300):
                done = agg.advance(new_fe)
                if done is not None:
                    self._emit(done)
        fe = self.finalized_end_s
        if fe is not None:
            self.next_due_ns = (fe + BASE_S) * _S + grace
            if self.marks:
                lo = fe * _S
                self.marks = [m for m in self.marks if m[1] >= lo]
        elif self.open:
            self.next_due_ns = (min(self.open) + BASE_S) * _S + grace
        else:
            self.next_due_ns = None

    def _finalize(self, acc: _Acc, s: int) -> None:
        extra = self._flags_for(s)
        if self.obs_start_ns is not None and s * _S < self.obs_start_ns:
            extra |= _PARTIAL
        cal = self.calendar
        td = ""
        if cal is not None and cal.valid:
            if cal.touches_boundary(s, s + BASE_S):
                extra |= _BOUNDARY
            td = cal.trading_date_for(s, s + BASE_S)
        bar = acc.freeze(self.instrument_id, BASE_S, extra, td, self.last_price)
        if bar is None:                                          # excluded-only prints before any price
            self.dropped_no_price += 1
            return
        if bar.trades:
            self.last_price = bar.close
        else:
            self.empty_bars += 1
        if bar.flags & _DATA_GAP:
            self.gap_bars += 1
        self._emit(bar)

    def _emit(self, bar: Bar) -> None:
        tf = bar.timeframe_s
        self.history[tf].append(bar)
        self.completed[tf] += 1
        agg = self._next_agg.get(tf)
        if agg is not None:
            for done in agg.add(bar):
                self._emit(done)

    # ------------------------------------------------------------------ views
    def completed_bars(self, tf: int) -> tuple[Bar, ...]:
        return tuple(self.history[tf])

    def forming(self) -> tuple[Bar | None, Bar | None, Bar | None]:
        """Forming 30 s / 1 m / 5 m bars (FORMING flag): completed children + open 30 s bars."""
        if self.open:
            ref = max(self.open)
        elif self._agg60.acc is not None or self._agg300.acc is not None:
            ref = (self.finalized_end_s or BASE_S) - BASE_S
        else:
            return None, None, None
        iid, lp = self.instrument_id, self.last_price
        cal = self.calendar
        td = cal.trading_date_for(ref, ref + BASE_S) if cal is not None and cal.valid else ""
        extra = _FORMING | self._flags_for(ref)
        f30 = self.open[ref].freeze(iid, 30, extra, td, lp) if ref in self.open else None
        opens = sorted(self.open)
        out: list[Bar | None] = [f30]
        for tf, parts in ((60, (self._agg60.acc,)), (300, (self._agg300.acc, self._agg60.acc))):
            p = ref - ref % tf
            acc = _Acc(p)
            for part in parts:
                if part is not None and p <= part.start_s < p + tf:
                    acc.merge(part)
            for s in opens:
                if p <= s < p + tf:
                    acc.merge(self.open[s])
            out.append(acc.freeze(iid, tf, extra, td, lp) if acc.children else None)
        return out[0], out[1], out[2]

    def token(self) -> tuple:
        return (self.completed[30], self.cond_flags, self.late_trades, self.armed)

    def snapshot(self, n: int) -> BarsSnapshot:
        c = self._snap
        if c is not None and c[0] == self._ver and c[1] == n:
            return c[2]                              # unchanged since the last snapshot: reuse (immutable)
        f30, f60, f300 = self.forming()
        h = self.history
        snap = BarsSnapshot(
            forming_30s=f30, forming_1m=f60, forming_5m=f300,
            latest_30s=tuple(islice(reversed(h[30]), n)), latest_1m=tuple(islice(reversed(h[60]), n)),
            latest_5m=tuple(islice(reversed(h[300]), n)),
            completed_30s=self.completed[30], completed_1m=self.completed[60], completed_5m=self.completed[300],
            finalized_through_s=self.finalized_end_s, late_trades=self.late_trades, late_volume=self.late_volume,
            excluded_trades=self.excluded_trades, excluded_volume=self.excluded_volume,
            excluded_by_reason=tuple(sorted(self.excluded_by_reason.items())),
            empty_bars=self.empty_bars, gap_bars=self.gap_bars, active_flags=BarFlag(self.cond_flags),
            close_grace_ms=self.cfg.close_grace_ms)
        self._snap = (self._ver, n, snap)
        return snap
