from hermes.market.classify import Aggressor
from hermes.market.patterns import FollowResult
from hermes.replay.runner import replay_session
from tests.support import BASE, BBO, DEPTH, TICK, TRADES, Harness, RawScript, write_hrec


def ready():
    sc = RawScript()
    sc.bootstrap()
    sc.seed_book(rows=10)
    sc.advance(600)
    sc.tick()
    return sc


def test_engine_exposes_patterns_snapshot():
    sc = ready()
    h = Harness().run(sc)
    p = h.engine.snapshot().instrument(1).patterns
    assert p is not None and p.available


def test_engine_detects_multi_price_buy_sweep():
    sc = ready()
    h = Harness().run(sc)
    start = len(sc.events)

    sc.advance(10)
    sc.trade(TRADES, BASE + TICK, 2)
    sc.advance(20)
    sc.trade(TRADES, BASE + 2 * TICK, 3)
    sc.advance(300)
    sc.tick()
    h.run(sc, start)

    p = h.engine.snapshot().instrument(1).patterns
    assert p.latest_sweeps
    sw = p.latest_sweeps[0]
    assert sw.aggressor is Aggressor.BUY
    assert sw.levels == 2
    assert sw.volume == 5


def test_engine_resolves_buy_follow_through_from_book_midpoint():
    sc = ready()
    h = Harness().run(sc)
    start = len(sc.events)

    sc.advance(10)
    sc.trade(TRADES, BASE + TICK, 4)
    sc.advance(100)
    # Move both best prices up one tick; keep row semantics simple via UPDATE.
    # Move best bid up one tick.
    sc.depth(DEPTH, 0, 1, 1, BASE + TICK, 10)

    # Remove the old best ask. The former row 1 (BASE + 2*TICK)
    # becomes the new best ask while preserving strict ask ordering.
    sc.depth(DEPTH, 0, 2, 0, 0.0, 0)

    # Keep the independent BBO reference aligned with the rebuilt book top.
    sc.bbo(BBO, BASE + TICK, BASE + 2 * TICK)
    sc.advance(500)
    sc.tick()
    h.run(sc, start)

    p = h.engine.snapshot().instrument(1).patterns
    assert p.latest_follow
    assert p.latest_follow[0].result is FollowResult.FOLLOW_THROUGH


def test_trade_resubscribe_clears_pending_pattern_attribution():
    sc = ready()
    h = Harness().run(sc)
    start = len(sc.events)

    sc.advance(10)
    sc.trade(TRADES, BASE + TICK, 4)
    h.run(sc, start)
    before = h.engine.snapshot().instrument(1).patterns
    assert before.available

    start = len(sc.events)
    sc.request("reqTickByTickData", 30_003, tick_type="AllLast")
    h.run(sc, start)
    after = h.engine.snapshot().instrument(1).patterns
    assert after.continuity_epoch == before.continuity_epoch + 1
    assert not after.latest_follow


def test_c8_patterns_replay_hash_is_deterministic(tmp_path):
    sc = ready()
    for i in range(16):
        sc.advance(30)
        if i % 4 in (0, 1):
            price = BASE + TICK * (1 + (i % 4))
            sc.trade(TRADES, price, 1 + i % 3)
        else:
            sc.trade(TRADES, BASE, 1)
        if i % 3 == 0:
            sc.advance(20)
            sc.depth(DEPTH, 0, 1, 1, BASE, 10 + i % 5)
    sc.advance(600)
    sc.tick()

    d = write_hrec(tmp_path / "c8-patterns", sc.events)
    a = replay_session(d)
    b = replay_session(d)

    pa = a.engine.snapshot().instrument(1).patterns
    pb = b.engine.snapshot().instrument(1).patterns
    assert pa == pb
    assert a.final_hash == b.final_hash
    assert [c.hash for c in a.checkpoints] == [c.hash for c in b.checkpoints]
