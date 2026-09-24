"""Deterministic trade aggressor classification (C4).

IBKR does NOT report the aggressor side. Hermès infers it from the tick-by-tick BidAsk
quote that existed BEFORE the trade in our observed (seq) ordering, with explicit handling of
the case where the quote moved before the trade callback reached us, and a tick-rule fallback.
UNKNOWN is a first-class result; nothing is forced into BUY or SELL.

Rules, in order (first match wins)
----------------------------------
0. ELIGIBILITY (``TapeConfig``): ``unreported`` prints, ``pastLimit`` prints and prints with a
   special condition not in ``allowed_special_conditions`` are stored but classified
   UNKNOWN(INELIGIBLE) and never update tick-rule state. Size <= 0 is also ineligible.
1. CONTEXT: quote-based inference requires a valid market context supplied by the engine
   (connected, farm OK, no 10197 conflict, live data, BBO stream active without error, active
   generation). Otherwise UNKNOWN(INVALID_CONTEXT).
2. NO QUOTE: no two-sided quote from the active BBO generation -> UNKNOWN(NO_QUOTE).
   Optional staleness (``max_quote_age_ms`` > 0; disabled by default because a quiet BBO is
   legitimate) -> UNKNOWN(STALE_QUOTE).
3. LOCKED/CROSSED quote (bid >= ask) -> UNKNOWN(LOCKED_OR_CROSSED).
4. price >= ask  -> BUY  (DIRECT_QUOTE)   | price <= bid -> SELL (DIRECT_QUOTE)
   ...unless a prior quote from the recent history (within ``ambiguity_window_ms`` before the
   trade, same generation) puts the price on the OPPOSITE side:
     - if no recent prior quote also supports the current side, the quote moved through the level
       before the trade callback reached us (e.g. old ask A, trade at A, new bid >= A => the level
       was lifted): the historical side is used with HISTORICAL_QUOTE confidence;
     - if recent prior quotes support BOTH sides (the quote oscillated through the level), the
       ordering is ambiguous -> UNKNOWN(AMBIGUOUS).
   Known limitation: a genuine print on the NEW side of a level that flipped within the window is
   labelled with the older side (HISTORICAL_QUOTE, reduced confidence). The window bounds this.
5. INSIDE THE SPREAD (bid < price < ask): if exactly one side is matched by a recent historical
   quote -> that side (HISTORICAL_QUOTE); both -> UNKNOWN(AMBIGUOUS); none -> tick rule (TICK_RULE)
   or UNKNOWN(NO_TICK_REFERENCE).

Tick rule: compares with the previous ELIGIBLE trade of the same trades generation:
price > prev -> BUY, < prev -> SELL, == prev -> last non-zero direction if one exists, else none.
Every eligible trade updates the tick state (whatever its classification). State resets on a
trades generation change, on connection loss/close, 10197 conflict and non-live data.

Confidence is a deterministic RANK (configurable), NOT a probability:
DIRECT_QUOTE 1.0 > HISTORICAL_QUOTE 0.6 > TICK_RULE 0.3 > UNKNOWN 0.0 (defaults).

``ambiguity_window_ms`` default 50 ms is a provisional C4 BASELINE chosen from one real MNQ
session (0 ms was overconfident, >= 250 ms admitted stale quotes); it is NOT optimized and will
be recalibrated on multiple sessions. ``ref_quote_age_ns`` (trade arrival - quote arrival of the
quote actually used) is recorded on every quote-based classification to support that.

No clocks, no randomness: every time value comes from event fields.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum

from hermes.config import TapeConfig

_MS = 1_000_000


class Aggressor(Enum):
    BUY = "buy"
    SELL = "sell"
    UNKNOWN = "unknown"


class ClassMethod(Enum):
    DIRECT_QUOTE = "direct_quote"          # prevailing quote at trade arrival
    HISTORICAL_QUOTE = "historical_quote"  # recent prior quote (quote moved before the trade callback)
    TICK_RULE = "tick_rule"
    NONE = "none"                      # UNKNOWN


class UnknownReason(Enum):
    INELIGIBLE = "ineligible"
    INVALID_CONTEXT = "invalid_context"
    NO_QUOTE = "no_quote"
    STALE_QUOTE = "stale_quote"
    LOCKED_OR_CROSSED = "locked_or_crossed"
    AMBIGUOUS = "ambiguous"
    NO_TICK_REFERENCE = "no_tick_reference"


@dataclass(frozen=True, slots=True)
class QuoteState:
    bid_units: int | None
    ask_units: int | None
    bid_size: int
    ask_size: int
    seq: int
    generation: int
    recv_mono_ns: int
    recv_wall_ns: int
    exch_ts_s: int

    @property
    def two_sided(self) -> bool:
        return self.bid_units is not None and self.ask_units is not None


@dataclass(frozen=True, slots=True)
class Classification:
    aggressor: Aggressor
    method: ClassMethod
    confidence: float
    reason: UnknownReason | None
    eligible: bool
    quote: QuoteState | None            # prevailing quote at arrival (None if none)
    ref_quote_seq: int | None           # quote actually used (differs from ``quote`` for HISTORICAL_QUOTE)
    ref_quote_age_ns: int | None = None # trade recv - recv of the quote actually used (calibration data)


def _allowed_conditions(cfg: TapeConfig) -> frozenset[str]:
    return frozenset(c.strip() for c in cfg.allowed_special_conditions.split(",") if c.strip())


class TradeClassifier:
    """Single-writer, deterministic. One instance per instrument."""

    __slots__ = ("_cfg", "_allowed", "_quotes", "_window_ns", "_max_quote_age_ns", "_quote_gen",
                 "_tick_prev", "_tick_dir", "_tick_gen", "epoch")

    def __init__(self, cfg: TapeConfig) -> None:
        self._cfg = cfg
        self._allowed = _allowed_conditions(cfg)
        self._quotes: deque[QuoteState] = deque(maxlen=cfg.quote_history)
        self._window_ns = cfg.ambiguity_window_ms * _MS
        self._max_quote_age_ns = cfg.max_quote_age_ms * _MS
        self._quote_gen: int | None = None
        self._tick_prev: int | None = None
        self._tick_dir: Aggressor | None = None
        self._tick_gen: int | None = None
        self.epoch = 0                       # increments on every reset (exposed in snapshots)

    # ------------------------------------------------------------------ state
    @property
    def current_quote(self) -> QuoteState | None:
        return self._quotes[-1] if self._quotes else None

    @property
    def quote_history_len(self) -> int:
        return len(self._quotes)

    @property
    def tick_direction(self) -> Aggressor | None:
        return self._tick_dir

    @property
    def tick_reference(self) -> int | None:
        return self._tick_prev

    def on_quote(self, q: QuoteState) -> None:
        if self._quote_gen != q.generation:
            self._quotes.clear()             # never mix quote generations
            self._quote_gen = q.generation
        self._quotes.append(q)

    def reset_quotes(self) -> None:
        if self._quotes or self._quote_gen is not None:
            self.epoch += 1
        self._quotes.clear()
        self._quote_gen = None

    def reset_tick(self) -> None:
        if self._tick_prev is not None or self._tick_dir is not None:
            self.epoch += 1
        self._tick_prev = None
        self._tick_dir = None
        self._tick_gen = None

    def reset_all(self) -> None:
        self.reset_quotes()
        self.reset_tick()

    # ------------------------------------------------------------------ eligibility
    def eligible(self, size: int, past_limit: bool, unreported: bool, special_conditions: str) -> bool:
        if size <= 0:
            return False
        if unreported and not self._cfg.classify_unreported:
            return False
        if past_limit and not self._cfg.classify_past_limit:
            return False
        conds = special_conditions.strip()
        if conds and not all(c.strip() in self._allowed for c in conds.split(",") if c.strip()):
            return False
        return True

    # ------------------------------------------------------------------ classification
    def classify(self, price: int, size: int, past_limit: bool, unreported: bool, special_conditions: str,
                 generation: int, now_mono_ns: int, context_ok: bool) -> Classification:
        cfg = self._cfg
        q = self.current_quote
        if not self.eligible(size, past_limit, unreported, special_conditions):
            return Classification(Aggressor.UNKNOWN, ClassMethod.NONE, 0.0, UnknownReason.INELIGIBLE, False, q, None)

        tick_side = self._tick_update(price, generation)   # always advance tick state for eligible prints

        if not context_ok:
            return self._unknown(UnknownReason.INVALID_CONTEXT, q)
        if q is None or not q.two_sided:
            return self._unknown(UnknownReason.NO_QUOTE, q)
        if self._max_quote_age_ns and now_mono_ns - q.recv_mono_ns > self._max_quote_age_ns:
            return self._unknown(UnknownReason.STALE_QUOTE, q)
        bid, ask = q.bid_units, q.ask_units
        if bid >= ask:  # type: ignore[operator]
            return self._unknown(UnknownReason.LOCKED_OR_CROSSED, q)

        if price >= ask:  # type: ignore[operator]
            return self._with_history_check(Aggressor.BUY, price, q, now_mono_ns)
        if price <= bid:  # type: ignore[operator]
            return self._with_history_check(Aggressor.SELL, price, q, now_mono_ns)

        # strictly inside the spread
        buy_ref, sell_ref = self._history_matches(price, q, now_mono_ns)
        if buy_ref is not None and sell_ref is None:
            return Classification(Aggressor.BUY, ClassMethod.HISTORICAL_QUOTE, cfg.confidence_historical_quote, None,
                                  True, q, buy_ref.seq, now_mono_ns - buy_ref.recv_mono_ns)
        if sell_ref is not None and buy_ref is None:
            return Classification(Aggressor.SELL, ClassMethod.HISTORICAL_QUOTE, cfg.confidence_historical_quote, None,
                                  True, q, sell_ref.seq, now_mono_ns - sell_ref.recv_mono_ns)
        if buy_ref is not None and sell_ref is not None:
            return self._unknown(UnknownReason.AMBIGUOUS, q)
        if tick_side is not None:
            return Classification(tick_side, ClassMethod.TICK_RULE, cfg.confidence_tick_rule, None, True, q, None)
        return self._unknown(UnknownReason.NO_TICK_REFERENCE, q)

    # ------------------------------------------------------------------ internals
    def _unknown(self, reason: UnknownReason, q: QuoteState | None) -> Classification:
        return Classification(Aggressor.UNKNOWN, ClassMethod.NONE, 0.0, reason, True, q, None)

    def _tick_update(self, price: int, generation: int) -> Aggressor | None:
        if self._tick_gen != generation:
            self._tick_prev = None
            self._tick_dir = None
            self._tick_gen = generation
        prev = self._tick_prev
        self._tick_prev = price
        if prev is None:
            return None
        if price > prev:
            self._tick_dir = Aggressor.BUY
        elif price < prev:
            self._tick_dir = Aggressor.SELL
        return self._tick_dir                # zero tick: last non-zero direction (or None)

    def _recent(self, current: QuoteState, now_mono_ns: int):
        """Prior two-sided quotes of the same generation within the window, newest first."""
        horizon = now_mono_ns - self._window_ns
        qs = self._quotes
        for i in range(len(qs) - 2, -1, -1):
            old = qs[i]
            if old.recv_mono_ns < horizon:
                break
            if old.two_sided and old.generation == current.generation:
                yield old

    def _history_matches(self, price: int, current: QuoteState, now_mono_ns: int
                         ) -> tuple[QuoteState | None, QuoteState | None]:
        buy_ref = sell_ref = None
        for old in self._recent(current, now_mono_ns):
            if buy_ref is None and price >= old.ask_units:  # type: ignore[operator]
                buy_ref = old
            if sell_ref is None and price <= old.bid_units:  # type: ignore[operator]
                sell_ref = old
        return buy_ref, sell_ref

    def _with_history_check(self, side: Aggressor, price: int, q: QuoteState, now_mono_ns: int) -> Classification:
        cfg = self._cfg
        buy_ref, sell_ref = self._history_matches(price, q, now_mono_ns)
        opposite, same = (sell_ref, buy_ref) if side is Aggressor.BUY else (buy_ref, sell_ref)
        if opposite is None:
            return Classification(side, ClassMethod.DIRECT_QUOTE, cfg.confidence_direct_quote, None, True, q, q.seq,
                                  now_mono_ns - q.recv_mono_ns)
        if same is not None:
            # recent quotes put this price on BOTH sides (quote oscillated through the level)
            return self._unknown(UnknownReason.AMBIGUOUS, q)
        # Only an older quote puts the print on the other side. For a print at/through the current
        # quote this implies the quote moved through that level in the direction the older side
        # would move it (e.g. old ask A lifted -> new bid >= A): the quote update overtook the
        # trade callback. Use the older side with reduced confidence.
        other = Aggressor.BUY if side is Aggressor.SELL else Aggressor.SELL
        return Classification(other, ClassMethod.HISTORICAL_QUOTE, cfg.confidence_historical_quote, None,
                              True, q, opposite.seq, now_mono_ns - opposite.recv_mono_ns)
