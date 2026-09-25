"""Order book unit tests: row semantics, structural violations, quality state machine."""

from __future__ import annotations

import pytest

from hermes.config import BookConfig
from hermes.market.events import BookSide, DepthOp, ResetReason
from hermes.market.orderbook import (
    BookState,
    InvalidationReason,
    LevelChange,
    OrderBook,
    QualityIssue,
    ViolationKind,
)

B, A = BookSide.BID, BookSide.ASK
INS, UPD, DEL = DepthOp.INSERT, DepthOp.UPDATE, DepthOp.DELETE
MS = 1_000_000


def cfg(**kw) -> BookConfig:
    base = dict(depth_rows=5, min_valid_rows=2, settle_ms=100, max_update_age_ms=1000,
                transient_grace_ms=50, bbo_tolerance_ticks=0, bbo_mismatch_grace_ms=200,
                escalate_after_ms=500, require_bbo_confirmation=True)
    base.update(kw)
    return BookConfig(**base)


def seed(book: OrderBook, t: int, bids=(100, 99), asks=(101, 102), size=10) -> None:
    for i, p in enumerate(bids):
        book.apply(B, INS, i, p, size, t)
    for i, p in enumerate(asks):
        book.apply(A, INS, i, p, size, t)


def valid_book(**kw) -> tuple[OrderBook, int]:
    book = OrderBook(cfg(**kw))
    t = 0
    seed(book, t)
    book.on_bbo(100, 101, t)
    t += 100 * MS
    book.evaluate(t)
    assert book.state is BookState.VALID, book.issues
    return book, t


# ---------------------------------------------------------------------------
# Row semantics
# ---------------------------------------------------------------------------

def test_initial_state():
    book = OrderBook(cfg())
    assert book.state is BookState.EMPTY
    assert book.best_bid() is None and book.spread_units() is None and book.mid_units() is None


def test_insert_shifts_down():
    book = OrderBook(cfg())
    book.apply(B, INS, 0, 100, 5, 0)
    book.apply(B, INS, 0, 101, 6, 0)          # new best bid pushes old one down
    book.apply(B, INS, 2, 98, 7, 0)           # append at end (pos == len)
    book.apply(B, INS, 2, 99, 8, 0)           # middle insert
    assert book.levels(B) == ((101, 6), (100, 5), (99, 8), (98, 7))
    assert book.state is BookState.BUILDING


def test_update_replaces_price_and_size():
    book = OrderBook(cfg())
    seed(book, 0)
    book.apply(B, UPD, 0, 100, 42, 0)
    assert book.best_bid() == (100, 42)
    book.apply(B, UPD, 1, 98, 3, 0)           # price change at a row
    assert book.levels(B) == ((100, 42), (98, 3))


def test_delete_shifts_up():
    book = OrderBook(cfg())
    seed(book, 0, bids=(100, 99, 98))
    book.apply(B, DEL, 0, 100, 0, 0)          # IBKR sends price/size with deletes; ignored
    assert book.levels(B) == ((99, 10), (98, 10))
    book.apply(B, DEL, 1, 0, 0, 0)
    assert book.levels(B) == ((99, 10),)


def test_insert_truncates_to_depth_rows():
    book = OrderBook(cfg(depth_rows=3, min_valid_rows=1))
    for i, p in enumerate((100, 99, 98)):
        book.apply(B, INS, i, p, 1, 0)
    changes = book.apply(B, INS, 0, 101, 2, 0)
    assert book.levels(B) == ((101, 2), (100, 1), (99, 1))
    assert book.counters.truncations == 1
    assert LevelChange(B, 98, 1, 0, True) in changes          # left through the window edge
    assert LevelChange(B, 101, 0, 2, False) in changes



def test_repeated_terminal_delete_is_opaque_tail_noop():
    """Regression for real CME/TWS sequence seen in the MNQ recording."""
    book = OrderBook(cfg(depth_rows=3, min_valid_rows=1))

    for i, price in enumerate((101, 102, 103)):
        book.apply(A, INS, i, price, 1, 0)

    # First DELETE removes the known terminal row.
    first = book.apply(A, DEL, 2, 0, 0, 1)
    assert first == (LevelChange(A, 103, 1, 0, True),)
    assert book.levels(A) == ((101, 1), (102, 1))

    # TWS may immediately repeat DELETE at the same requested position.
    # It refers to the opaque tail, not one of the known rows.
    assert book.apply(A, DEL, 2, 0, 0, 2) == ()
    assert book.levels(A) == ((101, 1), (102, 1))
    assert book.state is not BookState.STALE
    assert book.counters.opaque_tail_deletes == 1
    assert book.counters.violations.get(
        ViolationKind.POSITION_OUT_OF_RANGE, 0
    ) == 0



@pytest.mark.parametrize("op,pos,rows", [
    (INS, 3, 2),     # pos > len
    (INS, -1, 0),
    (INS, 5, 5),     # pos >= depth_rows (full side)
    (UPD, 2, 2),     # pos == len
    (UPD, 0, 0),     # update on empty side
    (DEL, 2, 2),
    (DEL, 0, 0),     # delete from empty side
    (DEL, -1, 2),
])
def test_out_of_range_positions_go_stale(op, pos, rows):
    book = OrderBook(cfg())
    for i in range(rows):
        book.apply(B, INS, i, 100 - i, 1, 0)
    book.apply(B, op, pos, 90, 1, 0)
    assert book.state is BookState.STALE
    assert book.needs_resync
    assert book.stale_reason is InvalidationReason.STRUCTURAL_VIOLATION
    assert book.counters.violations[ViolationKind.POSITION_OUT_OF_RANGE] == 1
    assert book.levels(B) == () and book.levels(A) == ()


def test_negative_size_invalid_side_invalid_op():
    for args, kind in [((B, INS, 0, 100, -1), ViolationKind.NEGATIVE_SIZE),
                       (("bid", INS, 0, 100, 1), ViolationKind.INVALID_SIDE),
                       ((B, 0, 0, 100, 1), ViolationKind.INVALID_OP)]:
        book = OrderBook(cfg())
        book.apply(*args, 0)  # type: ignore[arg-type]
        assert book.state is BookState.STALE
        assert book.counters.violations == {kind: 1}


def test_stale_ignores_ops_until_reset():
    book = OrderBook(cfg())
    book.apply(B, DEL, 0, 0, 0, 0)            # structural violation on empty book
    assert book.state is BookState.STALE
    assert book.apply(B, INS, 0, 100, 1, 1) == ()
    assert book.counters.ignored_while_stale == 1
    assert book.levels(B) == ()
    epoch = book.epoch
    book.reset(ResetReason.RESUBSCRIBE, 2)
    assert book.state is BookState.BUILDING and not book.needs_resync
    assert book.stale_reason is None and book.epoch == epoch + 1
    book.apply(B, INS, 0, 100, 1, 3)
    assert book.levels(B) == ((100, 1),)


# ---------------------------------------------------------------------------
# Level-change diff (liquidity changes from price->size maps, never op type)
# ---------------------------------------------------------------------------

def test_level_changes():
    book = OrderBook(cfg())
    assert book.apply(B, INS, 0, 100, 5, 0) == (LevelChange(B, 100, 0, 5),)
    assert book.apply(B, UPD, 0, 100, 8, 0) == (LevelChange(B, 100, 5, 8),)
    # price-changing update = one level removed + one added
    assert book.apply(B, UPD, 0, 101, 8, 0) == (LevelChange(B, 101, 0, 8), LevelChange(B, 100, 8, 0))
    assert book.apply(B, DEL, 0, 0, 0, 0) == (LevelChange(B, 101, 8, 0),)
    # size-unchanged update produces no change
    book.apply(A, INS, 0, 105, 3, 0)
    assert book.apply(A, UPD, 0, 105, 3, 0) == ()


def test_tail_refill_flagged_as_window_edge():
    book = OrderBook(cfg(depth_rows=3, min_valid_rows=1))
    for i, p in enumerate((101, 102, 103)):
        book.apply(A, INS, i, p, 1, 0)
    assert book.apply(A, DEL, 0, 0, 0, 0) == (LevelChange(A, 101, 1, 0, False),)
    # IBKR then refills the now-free last row with a level that was always there
    assert book.apply(A, INS, 2, 104, 9, 0) == (LevelChange(A, 104, 0, 9, True),)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def test_analytics():
    book = OrderBook(cfg())
    seed(book, 0, bids=(100, 99, 98), asks=(101, 103), size=4)
    assert book.best_bid() == (100, 4) and book.best_ask() == (101, 4)
    assert book.spread_units() == 1
    assert book.mid_units_x2() == 201 and book.mid_units() == 100.5
    assert book.levels(B, 2) == ((100, 4), (99, 4))
    assert book.total_size(B) == 12 and book.total_size(B, 1) == 4 and book.total_size(A) == 8


def test_snapshot_is_immutable_copy():
    book, t = valid_book()
    snap = book.snapshot()
    book.apply(B, UPD, 0, 100, 99, t)
    assert snap.bids[0] == (100, 10)
    assert snap.is_valid and snap.epoch == 0 and snap.issues == frozenset()
    with pytest.raises(Exception):
        snap.state = BookState.STALE  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Quality state machine
# ---------------------------------------------------------------------------

def test_becomes_valid_only_after_settle():
    book = OrderBook(cfg())
    seed(book, 0)
    book.on_bbo(100, 101, 0)
    assert book.state is BookState.BUILDING and book.issues == frozenset()
    book.evaluate(99 * MS)
    assert book.state is BookState.BUILDING
    book.evaluate(100 * MS)
    assert book.state is BookState.VALID


def test_valid_without_full_depth():
    # 10 rows configured, only 3 per side populated, min_valid_rows = 3 -> VALID
    book = OrderBook(cfg(depth_rows=10, min_valid_rows=3))
    seed(book, 0, bids=(100, 99, 98), asks=(101, 102, 103))
    book.on_bbo(100, 101, 0)
    book.evaluate(100 * MS)
    assert book.state is BookState.VALID


def test_insufficient_depth_and_recovery():
    book = OrderBook(cfg())
    seed(book, 0, bids=(100,), asks=(101, 102))
    book.on_bbo(100, 101, 0)
    book.evaluate(500 * MS)
    assert book.state is BookState.BUILDING and QualityIssue.INSUFFICIENT_DEPTH in book.issues

    book, t = valid_book()
    book.apply(A, DEL, 1, 0, 0, t)                         # asks drop to 1 row
    assert book.state is BookState.SUSPECT and QualityIssue.INSUFFICIENT_DEPTH in book.issues
    book.apply(A, INS, 1, 102, 5, t + 10 * MS)
    assert book.state is BookState.SUSPECT                 # must re-settle
    book.evaluate(t + 110 * MS)
    assert book.state is BookState.VALID


def test_depth_reset_317_rebuild():
    book, t = valid_book()
    epoch = book.epoch
    book.reset(ResetReason.IBKR_317, t)
    assert book.state is BookState.BUILDING and book.epoch == epoch + 1
    assert book.levels(B) == () and book.levels(A) == () and not book.is_valid
    assert book.counters.resets[ResetReason.IBKR_317] == 1
    # partial rebuild: one side only -> still not valid, even after long wait
    book.apply(B, INS, 0, 100, 1, t + MS)
    book.apply(B, INS, 1, 99, 1, t + MS)
    book.evaluate(t + 300 * MS)
    assert book.state is BookState.BUILDING
    # complete rebuild -> valid only after settle
    book.apply(A, INS, 0, 101, 1, t + 301 * MS)
    book.apply(A, INS, 1, 102, 1, t + 301 * MS)
    book.evaluate(t + 400 * MS)
    assert book.state is BookState.BUILDING
    book.evaluate(t + 401 * MS)
    assert book.state is BookState.VALID


def test_transient_cross_tolerated_within_grace():
    book, t = valid_book()
    book.apply(B, INS, 0, 101, 1, t)                        # bid 101 == ask 101 (locked)
    assert book.state is BookState.VALID
    book.evaluate(t + 49 * MS)
    assert book.state is BookState.VALID
    book.apply(B, DEL, 0, 0, 0, t + 49 * MS)                # race resolves before grace expires
    book.evaluate(t + 60 * MS)
    assert book.state is BookState.VALID


def test_persistent_cross_suspect_then_stale():
    book, t = valid_book()
    book.apply(B, INS, 0, 102, 1, t)                        # crossed: bid 102 > ask 101
    book.evaluate(t + 50 * MS)
    assert book.state is BookState.SUSPECT and QualityIssue.CROSSED in book.issues
    book.evaluate(t + 500 * MS)
    assert book.state is BookState.STALE
    assert book.stale_reason is InvalidationReason.PERSISTENT_CROSSED and book.needs_resync


def test_unsorted_grace_and_escalation():
    book, t = valid_book()
    book.apply(B, UPD, 1, 100, 1, t)                        # duplicate price 100 on bid side
    assert book.state is BookState.VALID
    book.evaluate(t + 50 * MS)
    assert book.state is BookState.SUSPECT and QualityIssue.UNSORTED in book.issues
    book.evaluate(t + 500 * MS)
    assert book.state is BookState.STALE and book.stale_reason is InvalidationReason.PERSISTENT_UNSORTED


def test_update_age():
    book, _ = valid_book()                                  # last depth update at t=0
    book.evaluate(1000 * MS)                                # age == limit -> still OK
    assert book.state is BookState.VALID
    book.evaluate(1001 * MS)
    assert book.state is BookState.SUSPECT and book.issues == {QualityIssue.UPDATE_AGE}
    book.apply(B, UPD, 0, 100, 11, 1002 * MS)
    book.evaluate(1102 * MS)
    assert book.state is BookState.VALID


def test_bbo_required():
    book = OrderBook(cfg())
    seed(book, 0)
    book.evaluate(1000 * MS)
    assert book.state is BookState.BUILDING and QualityIssue.BBO_UNAVAILABLE in book.issues


def test_bbo_not_required():
    book = OrderBook(cfg(require_bbo_confirmation=False))
    seed(book, 0)
    book.evaluate(100 * MS)
    assert book.state is BookState.VALID


def test_bbo_stream_lost_blocks_validity():
    book, t = valid_book()
    book.set_bbo_unavailable(t)
    assert book.state is BookState.SUSPECT and QualityIssue.BBO_UNAVAILABLE in book.issues
    book.on_bbo(100, None, t)                               # one-sided BBO is not a reference
    assert QualityIssue.BBO_UNAVAILABLE in book.issues


def test_bbo_mismatch_grace_and_escalation():
    book, t = valid_book()
    book.on_bbo(99, 100, t)                                 # BBO moved; depth not yet
    book.evaluate(t + 199 * MS)
    assert book.state is BookState.VALID                    # within sync tolerance
    book.evaluate(t + 200 * MS)
    assert book.state is BookState.SUSPECT and QualityIssue.BBO_MISMATCH in book.issues
    book.evaluate(t + 500 * MS)
    assert book.state is BookState.STALE
    assert book.stale_reason is InvalidationReason.PERSISTENT_BBO_MISMATCH


def test_bbo_mismatch_timer_restarts_on_agreement():
    book, t = valid_book()
    book.on_bbo(99, 100, t)
    book.on_bbo(100, 101, t + 150 * MS)                     # agrees again
    book.on_bbo(99, 100, t + 160 * MS)
    book.evaluate(t + 300 * MS)                             # only 140 ms of continuous mismatch
    assert book.state is BookState.VALID


def test_bbo_tolerance_ticks():
    book, t = valid_book(bbo_tolerance_ticks=1)
    book.on_bbo(99, 102, t)
    book.evaluate(t + 400 * MS)
    assert book.state is BookState.VALID
    book.on_bbo(98, 102, t + 400 * MS)
    book.evaluate(t + 600 * MS)
    assert book.state is BookState.SUSPECT


@pytest.mark.parametrize("reason", list(InvalidationReason))
def test_invalidate(reason):
    book, t = valid_book()
    book.invalidate(reason, t)
    assert book.state is BookState.STALE and book.needs_resync and book.stale_reason is reason
    book.invalidate(InvalidationReason.MANUAL, t)           # second invalidation keeps first reason
    assert book.stale_reason is reason
    book.evaluate(t + 10_000 * MS)
    assert book.state is BookState.STALE


def test_transition_history_bounded_and_recorded():
    book = OrderBook(cfg(), transition_history=3)
    for k in range(5):
        book.invalidate(InvalidationReason.MANUAL, k)
        book.reset(ResetReason.RESUBSCRIBE, k)
    assert len(book.transitions) == 3
    assert book.counters.transitions == 10
    assert book.transitions[-1].to_state is BookState.BUILDING


def test_determinism():
    def run() -> list:
        book = OrderBook(cfg())
        out = []
        ops = [(B, INS, 0, 100, 1), (A, INS, 0, 101, 2), (B, INS, 1, 99, 3), (A, INS, 1, 102, 4),
               (B, UPD, 0, 100, 7), (A, DEL, 0, 0, 0), (A, INS, 0, 101, 1)]
        for i, op in enumerate(ops):
            out.append(book.apply(*op, i * 30 * MS))
            if i == 3:
                book.on_bbo(100, 101, i * 30 * MS)
            out.append(book.snapshot())
        return out
    assert run() == run()
