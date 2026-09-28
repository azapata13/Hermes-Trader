from hermes.market.absorption import AbsorptionSnapshot
from tests.support import BASE, DEPTH, TICK, TRADES, Harness, RawScript


def ready():
    sc = RawScript()
    sc.bootstrap()
    sc.seed_book(rows=10)
    sc.advance(600)
    sc.tick()
    return sc


def test_engine_snapshot_exposes_pure_derived_absorption_context():
    sc = ready()
    h = Harness().run(sc)
    snap = h.engine.snapshot().instrument(1)
    assert isinstance(snap.absorption, AbsorptionSnapshot)
    assert snap.absorption.available
    assert [w.seconds for w in snap.absorption.windows] == [1, 5, 30]


def test_engine_absorption_context_combines_hit_replenishment_and_no_follow():
    sc = ready()
    h = Harness().run(sc)
    start = len(sc.events)

    sc.advance(10)
    sc.trade(TRADES, BASE + TICK, 4)  # known BUY at best ask
    sc.advance(50)
    sc.depth(DEPTH, 0, 1, 0, BASE + TICK, 14)  # same-price ask replenishment +4
    sc.advance(600)
    sc.tick()  # midpoint stayed flat -> BUY no-follow after horizon
    h.run(sc, start)

    a = h.engine.snapshot().instrument(1).absorption
    w = a.windows[0]
    assert w.buy_vs_ask.aggressive_volume == 4
    assert w.buy_vs_ask.replenished_volume == 4
    assert w.buy_vs_ask.no_follow_volume == 4
    assert w.buy_vs_ask.compatible_cap_volume == 4
