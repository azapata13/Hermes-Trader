"""Property-based tests: OrderBook vs a naive reference model of IBKR row semantics."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hermes.config import BookConfig
from hermes.market.events import BookSide, DepthOp, ResetReason
from hermes.market.orderbook import BookState, OrderBook, QualityIssue

MS = 1_000_000
MAX_ROWS = 6
SIDES = (BookSide.BID, BookSide.ASK)


def cfg() -> BookConfig:
    return BookConfig(depth_rows=MAX_ROWS, min_valid_rows=2, settle_ms=100, max_update_age_ms=10_000,
                      transient_grace_ms=50, bbo_tolerance_ticks=0, bbo_mismatch_grace_ms=200,
                      escalate_after_ms=500, require_bbo_confirmation=True)


def ref_apply(rows: list, op, pos: int, price: int, size: int) -> bool:
    """Naive reference implementation. Returns False for an invalid operation (rows untouched)."""
    n = len(rows)
    if size < 0:
        return False
    if op is DepthOp.INSERT:
        if not (0 <= pos <= n and pos < MAX_ROWS):
            return False
        rows.insert(pos, (price, size))
        while len(rows) > MAX_ROWS:
            rows.pop()
        return True
    if op is DepthOp.UPDATE:
        if not (0 <= pos < n):
            return False
        rows[pos] = (price, size)
        return True
    if op is DepthOp.DELETE:
        if not (0 <= pos < n):
            return False
        rows.pop(pos)
        return True
    return False


# ---------------------------------------------------------------------------
# (a) arbitrary VALID-position operations -> identical rows, never STALE
# ---------------------------------------------------------------------------

@settings(max_examples=300)
@given(st.data())
def test_matches_reference_for_valid_ops(data):
    book = OrderBook(cfg())
    ref = {BookSide.BID: [], BookSide.ASK: []}
    for _ in range(data.draw(st.integers(1, 60))):
        side = data.draw(st.sampled_from(SIDES))
        rows = ref[side]
        choices = [DepthOp.INSERT] + ([DepthOp.UPDATE, DepthOp.DELETE] if rows else [])
        op = data.draw(st.sampled_from(choices))
        if op is DepthOp.INSERT:
            pos = data.draw(st.integers(0, min(len(rows), MAX_ROWS - 1)))
        else:
            pos = data.draw(st.integers(0, len(rows) - 1))
        price = data.draw(st.integers(0, 40))
        size = data.draw(st.integers(0, 50))
        assert ref_apply(rows, op, pos, price, size)
        book.apply(side, op, pos, price, size, 0)  # constant time: grace/escalation never elapse
        assert list(book.levels(BookSide.BID)) == ref[BookSide.BID]
        assert list(book.levels(BookSide.ASK)) == ref[BookSide.ASK]
        assert book.state is not BookState.STALE
        assert len(book.levels(side)) <= MAX_ROWS


# ---------------------------------------------------------------------------
# (b) well-formed, exchange-like feed derived from a true price-level book
# ---------------------------------------------------------------------------

def _better(side: BookSide, p: int, q: int) -> bool:
    return p > q if side is BookSide.BID else p < q


def transform_ops(side: BookSide, cur: list, target: list, use_price_update) -> list:
    """IBKR-style row ops turning ``cur`` into ``target`` (both sorted best-first)."""
    cur = list(cur)
    ops = []
    target_prices = {p for p, _ in target}
    i = 0
    while i < len(target):
        tp, ts = target[i]
        if i >= len(cur):
            ops.append((DepthOp.INSERT, i, tp, ts))
            ref_apply(cur, DepthOp.INSERT, i, tp, ts)
            i += 1
            continue
        cp, cs = cur[i]
        if cp == tp:
            if cs != ts:
                ops.append((DepthOp.UPDATE, i, tp, ts))
                cur[i] = (tp, ts)
            i += 1
            continue
        cur_prices = {p for p, _ in cur}
        order_ok = (i == 0 or _better(side, cur[i - 1][0], tp)) and (i + 1 >= len(cur) or _better(side, tp, cur[i + 1][0]))
        if cp not in target_prices and tp not in cur_prices and order_ok and use_price_update():
            ops.append((DepthOp.UPDATE, i, tp, ts))    # price-changing update (IBKR does this)
            cur[i] = (tp, ts)
            i += 1
        elif _better(side, cp, tp):
            ops.append((DepthOp.DELETE, i, 0, 0))       # cp is no longer in the target
            ref_apply(cur, DepthOp.DELETE, i, 0, 0)
        else:
            ops.append((DepthOp.INSERT, i, tp, ts))     # tp missing here
            ref_apply(cur, DepthOp.INSERT, i, tp, ts)
            i += 1
    while len(cur) > len(target):
        ops.append((DepthOp.DELETE, len(cur) - 1, 0, 0))
        cur.pop()
    assert cur == target
    return ops


def side_levels(side: BookSide):
    prices = st.integers(1, 50) if side is BookSide.BID else st.integers(51, 100)
    return st.lists(st.tuples(prices, st.integers(1, 99)), max_size=MAX_ROWS, unique_by=lambda t: t[0]).map(
        lambda lv: sorted(lv, key=lambda t: -t[0] if side is BookSide.BID else t[0]))


@settings(max_examples=300)
@given(st.data())
def test_well_formed_feed_reproduces_true_book(data):
    book = OrderBook(cfg())
    now = 0
    for _ in range(data.draw(st.integers(1, 25))):
        for side in SIDES:
            target = data.draw(side_levels(side))
            ops = transform_ops(side, list(book.levels(side)), target, lambda: data.draw(st.booleans()))
            for op, pos, price, size in ops:
                now += MS
                book.apply(side, op, pos, price, size, now)
                assert book.state is not BookState.STALE
                assert not ({QualityIssue.UNSORTED, QualityIssue.CROSSED} & book.issues)
            assert list(book.levels(side)) == target
    # With a matching BBO reference, a sufficiently deep book validates after the settle period.
    bids, asks = book.levels(BookSide.BID), book.levels(BookSide.ASK)
    if len(bids) >= 2 and len(asks) >= 2:
        book.on_bbo(bids[0][0], asks[0][0], now)
        book.evaluate(now + 100 * MS)
        assert book.state is BookState.VALID


# ---------------------------------------------------------------------------
# (c) arbitrary garbage -> never raises, fails safe, recovers after reset
# ---------------------------------------------------------------------------

@settings(max_examples=300)
@given(st.lists(st.tuples(
    st.sampled_from(SIDES),
    st.sampled_from([DepthOp.INSERT, DepthOp.UPDATE, DepthOp.DELETE]),
    st.integers(-2, MAX_ROWS + 3),
    st.integers(-5, 60),
    st.integers(-2, 20),
), max_size=80))
def test_garbage_never_raises_and_fails_safe(ops):
    book = OrderBook(cfg())
    ref = {BookSide.BID: [], BookSide.ASK: []}
    violated = False
    for i, (side, op, pos, price, size) in enumerate(ops):
        book.apply(side, op, pos, price, size, i * MS)
        if not violated:
            if ref_apply(ref[side], op, pos, price, size):
                assert list(book.levels(BookSide.BID)) == ref[BookSide.BID]
                assert list(book.levels(BookSide.ASK)) == ref[BookSide.ASK]
            else:
                violated = True
        if violated:
            assert book.state is BookState.STALE and book.needs_resync
            assert book.levels(BookSide.BID) == () and book.levels(BookSide.ASK) == ()
    book.reset(ResetReason.RESUBSCRIBE, len(ops) * MS)
    assert book.state is BookState.BUILDING and not book.needs_resync
    book.apply(BookSide.BID, DepthOp.INSERT, 0, 10, 1, len(ops) * MS)
    assert book.levels(BookSide.BID) == ((10, 1),)
