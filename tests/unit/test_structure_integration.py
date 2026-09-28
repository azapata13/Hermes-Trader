from hermes.replay.runner import replay_session
from tests.support import BASE, DEPTH, TICK, TRADES, Harness, RawScript, write_hrec


def ready():
    sc = RawScript()
    sc.bootstrap()
    sc.seed_book(rows=10)
    sc.advance(600)
    sc.tick()
    return sc


def test_engine_exposes_structure_and_tracks_visible_add():
    sc = ready()
    h = Harness().run(sc)
    assert h.engine.snapshot().instrument(1).structure.available

    start = len(sc.events)
    sc.advance(50)
    sc.depth(DEPTH, 0, 1, 1, BASE, 20)
    h.run(sc, start)

    w = h.engine.snapshot().instrument(1).structure.windows[0]
    assert w.added_bid == 10


def test_engine_links_known_buy_to_same_price_ask_replenishment():
    sc = ready()
    h = Harness().run(sc)
    start = len(sc.events)

    sc.advance(50)
    sc.trade(TRADES, BASE + TICK, 4)
    sc.advance(50)
    sc.depth(DEPTH, 0, 1, 0, BASE + TICK, 14)
    h.run(sc, start)

    w = h.engine.snapshot().instrument(1).structure.windows[0]
    assert w.known_buy_at_ask == 4
    assert w.replenished_ask == 4
    assert w.replenish_events_ask == 1


def test_cme_terminal_delete_does_not_false_break_structure():
    sc = ready()
    h = Harness().run(sc)
    epoch = h.engine.snapshot().instrument(1).structure.continuity_epoch
    start = len(sc.events)

    sc.depth(DEPTH, 9, 2, 0, 0.0, 0)
    sc.depth(DEPTH, 9, 2, 0, 0.0, 0)
    sc.advance(100)
    sc.tick()
    h.run(sc, start)

    s = h.engine.snapshot().instrument(1).structure
    assert s.available
    assert s.continuity_epoch == epoch
    assert s.windows[0].edge_visibility_events >= 1


def test_c8_structure_replay_hash_is_deterministic(tmp_path):
    sc = ready()
    for i in range(12):
        sc.advance(40)
        sc.trade(TRADES, BASE + TICK if i % 2 == 0 else BASE, 1 + i % 2)
        sc.advance(20)
        side = 0 if i % 2 == 0 else 1
        price = BASE + TICK if side == 0 else BASE
        sc.depth(DEPTH, 0, 1, side, price, 10 + (i % 4))
    sc.advance(500)
    sc.tick()

    d = write_hrec(tmp_path / 'c8', sc.events)
    a = replay_session(d)
    b = replay_session(d)

    assert a.engine.snapshot().instrument(1).structure == b.engine.snapshot().instrument(1).structure
    assert a.final_hash == b.final_hash
    assert [c.hash for c in a.checkpoints] == [c.hash for c in b.checkpoints]
