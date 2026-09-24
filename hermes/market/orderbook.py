"""In-memory order book with IBKR row-position semantics and an explicit quality state machine.

See docs/ARCHITECTURE_PHASE_C.md §6.

Row semantics (IBKR depth is ROW-indexed, not price-indexed)
------------------------------------------------------------
Each side is a list of rows ``(price_units, size)``, best first, at most ``depth_rows`` long.

* INSERT  valid when ``0 <= pos <= len`` and ``pos < depth_rows``; rows below shift down;
          the side is truncated back to ``depth_rows``.
* UPDATE  valid when ``0 <= pos < len``; replaces price AND size (the price may change).
* DELETE  valid when ``0 <= pos < len``; rows below shift up.

Anything else (or a negative size / unknown side / unknown op) is a STRUCTURAL violation: the
row array is no longer knowable, so the book goes STALE immediately, is cleared, ignores row
ops until ``reset()``, and raises ``needs_resync``.

Quality (validity) — all conditions required, evaluated on every book/BBO event and on
``evaluate(now_ns)`` (clock ticks):

* each side has >= ``min_valid_rows`` rows (full depth NOT required)     -> INSUFFICIENT_DEPTH
* both sides strictly sorted (bids descending, asks ascending)           -> UNSORTED  (grace, escalates)
* not crossed / locked (best bid < best ask)                             -> CROSSED   (grace, escalates)
* last depth update age <= ``max_update_age_ms`` (optional, 0 = disabled) -> UPDATE_AGE
* tick-by-tick BBO reference available (if required)                     -> BBO_UNAVAILABLE
* |book top - BBO| <= ``bbo_tolerance_ticks`` on both sides              -> BBO_MISMATCH (grace, escalates)

``BUILDING``/``SUSPECT`` become ``VALID`` only after every condition has held continuously
for ``settle_ms``. Escalation (a graced issue persisting for ``escalate_after_ms``) moves the
book to STALE + ``needs_resync``.

Determinism: the book never reads a clock. Every time value is supplied by the caller
(event ``recv_mono_ns`` or a clock-tick event), so replay reproduces identical transitions.
The book is single-writer and not thread-safe by design (architecture §2).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from hermes.config import BookConfig
from hermes.market.events import BookSide, DepthOp, ResetReason

_MS = 1_000_000

Row = tuple[int, int]  # (price_units, size)


class BookState(Enum):
    EMPTY = "empty"          # nothing received since construction
    BUILDING = "building"    # receiving data after reset/subscribe; not yet validated
    VALID = "valid"          # all conditions held for settle_ms
    SUSPECT = "suspect"      # was valid; a condition currently fails (recoverable)
    STALE = "stale"          # content untrusted; cleared; needs reset/resync


class QualityIssue(Enum):
    INSUFFICIENT_DEPTH = "insufficient_depth"
    UNSORTED = "unsorted"
    CROSSED = "crossed"
    UPDATE_AGE = "update_age"
    BBO_UNAVAILABLE = "bbo_unavailable"
    BBO_MISMATCH = "bbo_mismatch"


class ViolationKind(Enum):
    POSITION_OUT_OF_RANGE = "position_out_of_range"
    NEGATIVE_SIZE = "negative_size"
    INVALID_SIDE = "invalid_side"
    INVALID_OP = "invalid_op"


class InvalidationReason(Enum):
    STRUCTURAL_VIOLATION = "structural_violation"
    PERSISTENT_CROSSED = "persistent_crossed"
    PERSISTENT_UNSORTED = "persistent_unsorted"
    PERSISTENT_BBO_MISMATCH = "persistent_bbo_mismatch"
    DISCONNECT = "disconnect"                  # connectionClosed / 1100
    DATA_LOST = "data_lost"                    # 1101
    SESSION_CONFLICT = "session_conflict"      # 10197
    DATA_NOT_LIVE = "data_not_live"            # delayed / frozen market data
    BACKLOG = "backlog"                        # dispatch backlog overflow
    DATA_ANOMALY = "data_anomaly"              # unusable depth row (off-grid price, bad code, fractional size)
    SUBSCRIPTION_FAILED = "subscription_failed"  # depth subscription rejected / halted
    INTERNAL_ERROR = "internal_error"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class LevelChange:
    """Visible liquidity change at one price, derived by diffing price->size maps.

    ``at_window_edge``: the level left or entered through the last visible row of a full side
    (truncation / tail refill). That is a VISIBILITY change, not necessarily add/cancel.
    """

    side: BookSide
    price_units: int
    old_size: int
    new_size: int
    at_window_edge: bool = False


@dataclass(frozen=True, slots=True)
class Transition:
    at_ns: int
    from_state: BookState
    to_state: BookState
    cause: str


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    instrument_id: int
    state: BookState
    issues: frozenset[QualityIssue]
    epoch: int
    bids: tuple[Row, ...]
    asks: tuple[Row, ...]
    last_update_ns: int | None
    needs_resync: bool
    stale_reason: InvalidationReason | None

    @property
    def is_valid(self) -> bool:
        return self.state is BookState.VALID


@dataclass(slots=True)
class BookCounters:
    ops: int = 0
    inserts: int = 0
    updates: int = 0
    deletes: int = 0
    truncations: int = 0
    ignored_while_stale: int = 0
    bbo_updates: int = 0
    violations: dict[ViolationKind, int] = field(default_factory=dict)
    resets: dict[ResetReason, int] = field(default_factory=dict)
    invalidations: dict[InvalidationReason, int] = field(default_factory=dict)
    transitions: int = 0


def _bump(d: dict, key: object) -> None:
    d[key] = d.get(key, 0) + 1


class OrderBook:
    """Single-instrument MBP book (IBKR market depth) + quality state machine."""

    __slots__ = (
        "instrument_id", "_cfg", "_max_rows", "_bids", "_asks", "_state", "_issues", "_epoch",
        "_last_update_ns", "_needs_resync", "_stale_reason", "_bbo_bid", "_bbo_ask",
        "_bbo_available", "_crossed_since", "_unsorted_since", "_bbo_mismatch_since", "_ok_since",
        "_settle_ns", "_max_age_ns", "_grace_ns", "_bbo_grace_ns", "_escalate_ns",
        "counters", "transitions",
    )

    def __init__(self, cfg: BookConfig, instrument_id: int = 1, transition_history: int = 64) -> None:
        self.instrument_id = instrument_id
        self._cfg = cfg
        self._max_rows = cfg.depth_rows
        self._bids: list[Row] = []
        self._asks: list[Row] = []
        self._state = BookState.EMPTY
        self._issues: frozenset[QualityIssue] = frozenset()
        self._epoch = 0
        self._last_update_ns: int | None = None
        self._needs_resync = False
        self._stale_reason: InvalidationReason | None = None
        self._bbo_bid: int | None = None
        self._bbo_ask: int | None = None
        self._bbo_available = False
        self._crossed_since: int | None = None
        self._unsorted_since: int | None = None
        self._bbo_mismatch_since: int | None = None
        self._ok_since: int | None = None
        self._settle_ns = cfg.settle_ms * _MS
        self._max_age_ns = cfg.max_update_age_ms * _MS
        self._grace_ns = cfg.transient_grace_ms * _MS
        self._bbo_grace_ns = cfg.bbo_mismatch_grace_ms * _MS
        self._escalate_ns = cfg.escalate_after_ms * _MS
        self.counters = BookCounters()
        self.transitions: deque[Transition] = deque(maxlen=transition_history)

    # ================================================================== mutation
    def apply(self, side: BookSide, op: DepthOp, position: int, price_units: int, size: int,
              now_ns: int) -> tuple[LevelChange, ...]:
        """Apply one IBKR depth row operation. Never raises on bad input (fails safe to STALE)."""
        if self._state is BookState.STALE:
            self.counters.ignored_while_stale += 1
            return ()

        if side is BookSide.BID:
            rows = self._bids
        elif side is BookSide.ASK:
            rows = self._asks
        else:
            self._structural(ViolationKind.INVALID_SIDE, now_ns)
            return ()

        n = len(rows)
        if op is DepthOp.INSERT:
            ok = 0 <= position <= n and position < self._max_rows
        elif op is DepthOp.UPDATE or op is DepthOp.DELETE:
            ok = 0 <= position < n
        else:
            self._structural(ViolationKind.INVALID_OP, now_ns)
            return ()
        if not ok:
            self._structural(ViolationKind.POSITION_OUT_OF_RANGE, now_ns)
            return ()
        if size < 0:
            self._structural(ViolationKind.NEGATIVE_SIZE, now_ns)
            return ()

        before = rows.copy()
        c = self.counters
        c.ops += 1
        if op is DepthOp.INSERT:
            c.inserts += 1
            rows.insert(position, (price_units, size))
            if len(rows) > self._max_rows:
                del rows[self._max_rows:]
                c.truncations += 1
        elif op is DepthOp.UPDATE:
            c.updates += 1
            rows[position] = (price_units, size)
        else:
            c.deletes += 1
            del rows[position]

        self._last_update_ns = now_ns
        if self._state is BookState.EMPTY:
            self._transition(BookState.BUILDING, now_ns, "first depth data")
        changes = self._diff(side, before, rows)
        self._evaluate(now_ns)
        return changes

    def on_bbo(self, bid_units: int | None, ask_units: int | None, now_ns: int) -> None:
        """Tick-by-tick BidAsk reference (primary BBO source, decision 1)."""
        self.counters.bbo_updates += 1
        self._bbo_bid = bid_units
        self._bbo_ask = ask_units
        self._bbo_available = bid_units is not None and ask_units is not None
        self._evaluate(now_ns)

    def set_bbo_unavailable(self, now_ns: int) -> None:
        """BBO stream rejected/lost: the book cannot be VALID while BBO confirmation is required."""
        self._bbo_bid = self._bbo_ask = None
        self._bbo_available = False
        self._bbo_mismatch_since = None
        self._evaluate(now_ns)

    def reset(self, reason: ResetReason, now_ns: int) -> None:
        """Clear both sides (e.g. IBKR error 317) and start rebuilding."""
        _bump(self.counters.resets, reason)
        self._bids.clear()
        self._asks.clear()
        self._epoch += 1
        self._needs_resync = False
        self._stale_reason = None
        self._crossed_since = self._unsorted_since = self._bbo_mismatch_since = self._ok_since = None
        self._last_update_ns = now_ns  # age measured from the reset
        self._transition(BookState.BUILDING, now_ns, f"reset:{reason.value}")
        self._evaluate(now_ns)

    def invalidate(self, reason: InvalidationReason, now_ns: int) -> None:
        """Mark content untrusted: STALE, cleared, row ops ignored until reset, resync requested."""
        _bump(self.counters.invalidations, reason)
        self._bids.clear()
        self._asks.clear()
        self._needs_resync = True
        self._crossed_since = self._unsorted_since = self._bbo_mismatch_since = self._ok_since = None
        if self._state is not BookState.STALE:
            self._stale_reason = reason
            self._issues = frozenset()
            self._transition(BookState.STALE, now_ns, f"invalidate:{reason.value}")

    def evaluate(self, now_ns: int) -> None:
        """Re-evaluate time-dependent conditions (call on clock ticks)."""
        self._evaluate(now_ns)

    # ================================================================== queries
    @property
    def state(self) -> BookState:
        return self._state

    @property
    def is_valid(self) -> bool:
        return self._state is BookState.VALID

    @property
    def issues(self) -> frozenset[QualityIssue]:
        return self._issues

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def needs_resync(self) -> bool:
        return self._needs_resync

    @property
    def stale_reason(self) -> InvalidationReason | None:
        return self._stale_reason

    @property
    def last_update_ns(self) -> int | None:
        return self._last_update_ns

    def best_bid(self) -> Row | None:
        return self._bids[0] if self._bids else None

    def best_ask(self) -> Row | None:
        return self._asks[0] if self._asks else None

    def spread_units(self) -> int | None:
        if not self._bids or not self._asks:
            return None
        return self._asks[0][0] - self._bids[0][0]

    def mid_units_x2(self) -> int | None:
        """Exact midpoint times two (integer), to avoid half-unit floats."""
        if not self._bids or not self._asks:
            return None
        return self._asks[0][0] + self._bids[0][0]

    def mid_units(self) -> float | None:
        m = self.mid_units_x2()
        return None if m is None else m / 2

    def levels(self, side: BookSide, n: int | None = None) -> tuple[Row, ...]:
        rows = self._bids if side is BookSide.BID else self._asks
        return tuple(rows if n is None else rows[:n])

    def total_size(self, side: BookSide, n: int | None = None) -> int:
        rows = self._bids if side is BookSide.BID else self._asks
        return sum(s for _, s in (rows if n is None else rows[:n]))

    def snapshot(self) -> BookSnapshot:
        return BookSnapshot(
            instrument_id=self.instrument_id,
            state=self._state,
            issues=self._issues,
            epoch=self._epoch,
            bids=tuple(self._bids),
            asks=tuple(self._asks),
            last_update_ns=self._last_update_ns,
            needs_resync=self._needs_resync,
            stale_reason=self._stale_reason,
        )

    # ================================================================== internals
    def _structural(self, kind: ViolationKind, now_ns: int) -> None:
        _bump(self.counters.violations, kind)
        self.invalidate(InvalidationReason.STRUCTURAL_VIOLATION, now_ns)

    def _transition(self, new: BookState, now_ns: int, cause: str) -> None:
        if new is self._state:
            return
        self.transitions.append(Transition(now_ns, self._state, new, cause))
        self.counters.transitions += 1
        self._state = new

    def _diff(self, side: BookSide, before: list[Row], after: list[Row]) -> tuple[LevelChange, ...]:
        full = self._max_rows
        bmap: dict[int, int] = {}
        bidx: dict[int, int] = {}
        for i, (p, s) in enumerate(before):
            bmap[p] = bmap.get(p, 0) + s
            bidx[p] = i
        amap: dict[int, int] = {}
        aidx: dict[int, int] = {}
        for i, (p, s) in enumerate(after):
            amap[p] = amap.get(p, 0) + s
            aidx[p] = i
        prices = set(bmap) | set(amap)
        out = []
        for p in sorted(prices, reverse=side is BookSide.BID):
            old = bmap.get(p, 0)
            new = amap.get(p, 0)
            if old == new:
                continue
            edge = False
            if p not in amap and len(before) == full and bidx[p] == full - 1:
                edge = True   # left through the last visible row of a full side
            elif p not in bmap and len(after) == full and aidx[p] == full - 1:
                edge = True   # entered through the last visible row of a full side
            out.append(LevelChange(side, p, old, new, edge))
        return tuple(out)

    @staticmethod
    def _sorted(rows: list[Row], descending: bool) -> bool:
        if descending:
            return all(rows[i][0] > rows[i + 1][0] for i in range(len(rows) - 1))
        return all(rows[i][0] < rows[i + 1][0] for i in range(len(rows) - 1))

    def _evaluate(self, now: int) -> None:
        st = self._state
        if st is BookState.STALE or st is BookState.EMPTY:
            return
        cfg = self._cfg
        issues: set[QualityIssue] = set()
        bids, asks = self._bids, self._asks

        # --- structural sanity with grace + escalation ---
        unsorted = not (self._sorted(bids, True) and self._sorted(asks, False))
        if unsorted:
            if self._unsorted_since is None:
                self._unsorted_since = now
            age = now - self._unsorted_since
            if age >= self._escalate_ns:
                self.invalidate(InvalidationReason.PERSISTENT_UNSORTED, now)
                return
            if age >= self._grace_ns:
                issues.add(QualityIssue.UNSORTED)
        else:
            self._unsorted_since = None

        crossed = bool(bids) and bool(asks) and bids[0][0] >= asks[0][0]
        if crossed:
            if self._crossed_since is None:
                self._crossed_since = now
            age = now - self._crossed_since
            if age >= self._escalate_ns:
                self.invalidate(InvalidationReason.PERSISTENT_CROSSED, now)
                return
            if age >= self._grace_ns:
                issues.add(QualityIssue.CROSSED)
        else:
            self._crossed_since = None

        # --- depth and freshness ---
        if len(bids) < cfg.min_valid_rows or len(asks) < cfg.min_valid_rows:
            issues.add(QualityIssue.INSUFFICIENT_DEPTH)
        # Optional (disabled by default, max_update_age_ms = 0): a quiet but correct book is not
        # evidence of failure (C3 amendment B). Stream age is primarily telemetry.
        if self._max_age_ns and (self._last_update_ns is None or now - self._last_update_ns > self._max_age_ns):
            issues.add(QualityIssue.UPDATE_AGE)

        # --- cross-check against tick-by-tick BBO ---
        if cfg.require_bbo_confirmation and not self._bbo_available:
            issues.add(QualityIssue.BBO_UNAVAILABLE)
            self._bbo_mismatch_since = None
        elif self._bbo_available and bids and asks:
            tol = cfg.bbo_tolerance_ticks
            mismatch = (abs(bids[0][0] - self._bbo_bid) > tol  # type: ignore[operator]
                        or abs(asks[0][0] - self._bbo_ask) > tol)  # type: ignore[operator]
            if mismatch:
                if self._bbo_mismatch_since is None:
                    self._bbo_mismatch_since = now
                age = now - self._bbo_mismatch_since
                if age >= self._escalate_ns:
                    self.invalidate(InvalidationReason.PERSISTENT_BBO_MISMATCH, now)
                    return
                if age >= self._bbo_grace_ns:
                    issues.add(QualityIssue.BBO_MISMATCH)
            else:
                self._bbo_mismatch_since = None
        else:
            self._bbo_mismatch_since = None

        self._issues = frozenset(issues)

        # --- state transitions ---
        if issues:
            self._ok_since = None
            if st is BookState.VALID:
                self._transition(BookState.SUSPECT, now, ",".join(sorted(i.value for i in issues)))
            return
        if self._ok_since is None:
            self._ok_since = now
        if st is not BookState.VALID and now - self._ok_since >= self._settle_ns:
            self._transition(BookState.VALID, now, "conditions held for settle period")
