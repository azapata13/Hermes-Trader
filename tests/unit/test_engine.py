"""MarketEngine: generations, health evidence, blocks, bounded 10197 recovery, snapshots."""

from __future__ import annotations

import threading

import pytest

from hermes.config import BookConfig, SessionConfig, SubscriptionsConfig
from hermes.market.engine import ALERT_CONFLICT_EXHAUSTED, ALERT_CONTRACT_FAILED
from hermes.market.events import ConnectionState, Stream, StreamStatus
from hermes.market.health import ConflictPhase
from hermes.market.orderbook import BookState
from tests.support import BBO, BASE, DEPTH, L1, TICK, TRADES, Harness, RawScript


def ready() -> tuple[Harness, RawScript]:
    s = RawScript().bootstrap().seed_book()
    s.advance(600)
    s.tick()
    h = Harness().run(s)
    inst = h.engine.snapshot().instrument(1)
    assert inst.market_data_ok, inst.not_ok_reasons
    return h, s


def step(h: Harness, s: RawScript, fn, *a, **kw):
    n = len(s.events)
    fn(*a, **kw)
    h.run(s, n)


def inst(h):
    return h.engine.snapshot().instrument(1)


# ---------------------------------------------------------------------------
# Startup and snapshot
# ---------------------------------------------------------------------------

def test_healthy_startup_snapshot():
    h, _ = ready()
    snap = h.engine.snapshot()
    i = snap.instrument(1)
    assert snap.connection is ConnectionState.CONNECTED and snap.alerts == ()
    assert i.contract_state == "defined" and i.local_symbol == "MNQZ6"
    assert i.book.state is BookState.VALID and i.market_data_type == 1
    st = {x.stream: x for x in i.streams}
    assert st[Stream.DEPTH].status is StreamStatus.ACTIVE and st[Stream.DEPTH].generation == DEPTH
    assert st[Stream.TRADES].status is StreamStatus.REQUESTED  # no trade yet: still OK


def test_not_ok_until_everything_proven():
    s = RawScript().bootstrap()
    h = Harness().run(s)
    reasons = inst(h).not_ok_reasons
    assert "depth:requested" in reasons and "l1:live_not_confirmed" in reasons and not inst(h).market_data_ok


# ---------------------------------------------------------------------------
# Amendment A: old-generation callbacks never mutate state
# ---------------------------------------------------------------------------

def test_late_depth_callbacks_after_resync_are_ignored():
    h, s = ready()
    before = inst(h).book
    step(h, s, s.request, "cancelMktDepth", DEPTH)
    step(h, s, s.request, "reqMktDepth", 20_001)
    assert inst(h).book.state is BookState.BUILDING and inst(h).book.bids == ()
    # late callbacks of the OLD generation arrive after the new subscription started
    for i in range(5):
        step(h, s, s.depth, DEPTH, 0, 0, 1, BASE + 10, 999)
    step(h, s, s.error, DEPTH, 317, "Market depth data has been RESET")
    assert inst(h).book.bids == () and inst(h).book.epoch == before.epoch + 1
    assert h.engine.counters.inactive_callbacks == 5
    # the new generation works
    step(h, s, s.depth, 20_001, 0, 0, 1, BASE, 3)
    assert inst(h).book.bids == ((84000, 3),)


@pytest.mark.parametrize("old,new,stream", [(BBO, 20_002, "BidAsk"), (TRADES, 20_003, "AllLast")])
def test_late_tick_by_tick_callbacks_ignored(old, new, stream):
    h, s = ready()
    assert inst(h).bbo is not None
    step(h, s, s.request, "cancelTickByTickData", old)
    step(h, s, s.request, "reqTickByTickData", new, tick_type=stream)
    if stream == "BidAsk":
        step(h, s, s.bbo, old, BASE - 5, BASE + 5)               # late callback, old generation
        assert inst(h).bbo is None                                # cleared by resubscribe; old not applied
        step(h, s, s.bbo, new, BASE, BASE + TICK)
        assert inst(h).bbo.bid_units == 84000
    else:
        step(h, s, s.trade, old, BASE + 5)
        assert inst(h).last_trade is None
        step(h, s, s.trade, new, BASE)
        assert inst(h).last_trade.price_units == 84000
    assert h.engine.counters.inactive_callbacks == 1


def test_late_l1_callbacks_ignored_and_live_needs_new_generation():
    h, s = ready()
    step(h, s, s.request, "cancelMktData", L1)
    step(h, s, s.request, "reqMktData", 20_004)
    step(h, s, s.mdt, L1, 1)                                         # old generation confirms LIVE
    assert "l1:live_not_confirmed" in inst(h).not_ok_reasons
    step(h, s, s.mdt, 20_004, 1)
    assert "l1:live_not_confirmed" not in inst(h).not_ok_reasons


def test_second_layer_generation_filter():
    """Even if a stale event reached the engine directly, it is rejected."""
    h, s = ready()
    from hermes.market import events as M
    from hermes.market.events import BookSide, DepthOp
    ev = M.DepthRowEvent(seq=10**6, instrument_id=1, recv_mono_ns=s.mono, recv_wall_ns=s.wall, generation=12345,
                         side=BookSide.BID, op=DepthOp.DELETE, position=0, price_units=0, size=0)
    h.engine.on_event(ev)
    assert h.engine.counters.stale_generation_rejected == 1 and len(inst(h).book.bids) == 5


# ---------------------------------------------------------------------------
# Amendment B: silence is not failure
# ---------------------------------------------------------------------------

def test_quiet_market_stays_valid():
    h, s = ready()
    for _ in range(12):           # 10 minutes of heartbeats and ticks, no market data at all
        s.advance(50_000)
        step(h, s, s.heartbeat)
        step(h, s, s.tick)
    i = inst(h)
    assert i.market_data_ok and i.book.state is BookState.VALID
    assert all(x.status is not StreamStatus.UNAVAILABLE for x in i.streams)


def test_bbo_frozen_evidence_is_telemetry_only():
    h, s = ready()
    s.advance(3000)
    step(h, s, s.trade, TRADES, BASE + 2.0)          # 8 ticks above a 3 s old BBO
    assert h.engine.counters.bbo_frozen_suspect == 1
    assert inst(h).market_data_ok


# ---------------------------------------------------------------------------
# Connection / farm / errors
# ---------------------------------------------------------------------------

def test_depth_reset_317_and_rebuild():
    h, s = ready()
    step(h, s, s.error, DEPTH, 317)
    assert inst(h).book.state is BookState.BUILDING and not inst(h).market_data_ok
    n = len(s.events)
    s.seed_book(l1=0)
    s.advance(600)
    s.tick()
    h.run(s, n)
    assert inst(h).book.state is BookState.VALID and inst(h).market_data_ok


def test_connectivity_lost_and_restored_data_lost():
    h, s = ready()
    step(h, s, s.error, -1, 1100)
    assert h.engine.connection is ConnectionState.LOST and inst(h).book.state is BookState.STALE
    assert "connection:lost" in inst(h).not_ok_reasons
    step(h, s, s.error, -1, 1101)
    assert h.engine.resubscribe_all_pending
    n = len(s.events)
    s.subscribe(30_001, 30_002, 30_003, 30_004)
    h.run(s, n)
    assert not h.engine.resubscribe_all_pending


def test_farm_broken_is_evidence():
    h, s = ready()
    step(h, s, s.error, -1, 2103, "Market data farm connection is broken:usfuture")
    i = inst(h)
    assert "farm:broken" in i.not_ok_reasons
    assert {x.status for x in i.streams if x.stream is not Stream.TRADES} == {StreamStatus.DEGRADED}
    step(h, s, s.error, -1, 2104)
    assert "farm:broken" not in inst(h).not_ok_reasons


def test_subscription_rejection_fail_safe():
    h, s = ready()
    step(h, s, s.error, BBO, 10190, "Max number of tick-by-tick requests has been reached")
    i = inst(h)
    assert {x.stream: x.status for x in i.streams}[Stream.BBO] is StreamStatus.UNAVAILABLE
    assert "bbo:error_10190" in i.not_ok_reasons and i.book.state is not BookState.VALID


def test_unknown_error_on_active_subscription_is_fail_safe_but_info_is_not():
    h, s = ready()
    step(h, s, s.error, TRADES, 2150, "some warning")
    assert inst(h).market_data_ok
    step(h, s, s.error, TRADES, 55555, "never seen before")
    assert "trades:error_55555" in inst(h).not_ok_reasons


def test_delayed_data_hard_block():
    h, s = ready()
    step(h, s, s.mdt, L1, 3)
    assert h.engine.not_live and inst(h).book.state is BookState.STALE
    assert all(x.status is StreamStatus.BLOCKED for x in inst(h).streams)
    step(h, s, s.mdt, L1, 1)
    assert not h.engine.not_live and not inst(h).market_data_ok   # book must be resynced first


def test_connection_closed():
    h, s = ready()
    step(h, s, s.closed)
    i = inst(h)
    assert h.engine.connection is ConnectionState.CLOSED and not i.market_data_ok
    assert all(x.generation is None for x in i.streams)
    step(h, s, s.depth, DEPTH, 0, 0, 1, BASE, 1)
    assert h.engine.counters.stale_generation_rejected == 1


def test_contract_failure_alert():
    s = RawScript()
    s.next_valid_id()
    from tests.support import SPEC
    s.request("reqContractDetails", 10_000, **dict(SPEC.to_params()))
    s.contract_end()
    h = Harness().run(s)
    assert ALERT_CONTRACT_FAILED in h.engine.alerts and inst(h).contract_state == "failed"
    assert ALERT_CONTRACT_FAILED in h.engine.new_alerts


# ---------------------------------------------------------------------------
# 10197 recovery: proven, never by time; bounded
# ---------------------------------------------------------------------------

def conflict(h, s):
    step(h, s, s.error, -1, 10197, "No market data during competing live session")


def attempt(h, s, base):
    step(h, s, s.control, "conflict_recovery_attempt")
    n = len(s.events)
    s.request("cancelMktDepth", None)
    s.subscribe(base + 1, base + 2, base + 3, base + 4)
    h.run(s, n)


def test_conflict_blocks_and_waiting_never_clears_it():
    h, s = ready()
    conflict(h, s)
    assert h.engine.conflict.phase is ConflictPhase.BLOCKED and not inst(h).market_data_ok
    assert all(x.status is StreamStatus.BLOCKED for x in inst(h).streams)
    for _ in range(20):
        s.advance(60_000)
        step(h, s, s.tick)
    assert h.engine.conflict.phase is ConflictPhase.BLOCKED


def test_conflict_recovery_requires_all_evidence():
    h, s = ready()
    conflict(h, s)
    attempt(h, s, 40_000)
    c = h.engine.conflict
    assert c.phase is ConflictPhase.RECOVERING and c.attempts == 1
    # depth + BBO data and LIVE from old generations do not count
    step(h, s, s.bbo, BBO, BASE, BASE + TICK)
    step(h, s, s.mdt, L1, 1)
    assert c.phase is ConflictPhase.RECOVERING
    # fresh depth + BBO on new generations, but LIVE not yet confirmed and book not settled
    n = len(s.events)
    s.seed_book(40_001, 40_002, l1=0)
    h.run(s, n)
    assert c.phase is ConflictPhase.RECOVERING
    blockers = h.engine.recovery_blockers(h.engine.instruments[1], c.attempt_started_seq)
    assert "l1:live_not_confirmed" in blockers and "book:not_valid" in blockers
    assert not any(b.startswith("trades") for b in blockers)           # no trade print required
    step(h, s, s.mdt, 40_004, 1)
    s.advance(600)
    step(h, s, s.tick)                                                   # book settles -> VALID
    assert c.phase is ConflictPhase.NONE and c.attempts == 0 and inst(h).market_data_ok


def test_conflict_during_attempt_fails_it():
    h, s = ready()
    conflict(h, s)
    attempt(h, s, 40_000)
    conflict(h, s)
    assert h.engine.conflict.phase is ConflictPhase.BLOCKED and h.engine.conflict.failures == 1


def test_attempt_timeout_fails_and_budget_exhausts_after_three():
    h, s = ready()
    conflict(h, s)
    for k in range(3):
        attempt(h, s, 40_000 + 10 * k)
        s.advance(20_000)
        step(h, s, s.tick)                                               # attempt timed out
    c = h.engine.conflict
    assert c.phase is ConflictPhase.EXHAUSTED and c.failures == 3
    assert ALERT_CONFLICT_EXHAUSTED in h.engine.alerts
    assert all(x.status is StreamStatus.UNAVAILABLE for x in inst(h).streams)
    assert not c.can_attempt()
    step(h, s, s.control, "conflict_recovery_attempt")                   # ignored: budget exhausted
    assert c.phase is ConflictPhase.EXHAUSTED and c.attempts == 3


def test_budget_resets_on_operator_retry_or_reconnect():
    h, s = ready()
    conflict(h, s)
    for k in range(3):
        attempt(h, s, 40_000 + 10 * k)
        s.advance(20_000)
        step(h, s, s.tick)
    step(h, s, s.control, "operator_retry")
    c = h.engine.conflict
    assert c.phase is ConflictPhase.BLOCKED and c.attempts == 0 and ALERT_CONFLICT_EXHAUSTED not in h.engine.alerts
    for k in range(3):
        attempt(h, s, 50_000 + 10 * k)
        s.advance(20_000)
        step(h, s, s.tick)
    assert c.phase is ConflictPhase.EXHAUSTED
    step(h, s, s.closed)
    step(h, s, s.next_valid_id)
    assert c.phase is ConflictPhase.BLOCKED and c.attempts == 0


# ---------------------------------------------------------------------------
# Single-writer guard and determinism
# ---------------------------------------------------------------------------

def test_owner_guard_rejects_foreign_thread():
    h, s = ready()
    owner = threading.get_ident()

    def guard():
        if threading.get_ident() != owner:
            raise RuntimeError("engine written from a foreign thread")

    h.engine.set_owner_guard(guard)
    step(h, s, s.tick)
    errors = []

    def foreign():
        try:
            step(h, s, s.tick)
        except RuntimeError as e:
            errors.append(e)

    t = threading.Thread(target=foreign)
    t.start()
    t.join()
    assert len(errors) == 1


def test_engine_replay_determinism():
    s = RawScript().bootstrap().seed_book()
    s.advance(600)
    s.tick()
    s.error(DEPTH, 317)
    s.seed_book(l1=0)
    s.error(-1, 10197)
    a = Harness().run(s)
    b = Harness().run(s)
    assert a.engine.snapshot() == b.engine.snapshot()
    assert a.market_events == b.market_events


def test_config_driven_required_streams():
    s = RawScript().bootstrap(l1=0).seed_book(l1=0)
    s.advance(600)
    s.tick()
    h = Harness(subs=SubscriptionsConfig(l1_market_data=False)).run(s)
    assert inst(h).market_data_ok
    h2 = Harness(book=BookConfig(), session=SessionConfig()).run(s)
    assert "l1:not_subscribed" in inst(h2).not_ok_reasons
