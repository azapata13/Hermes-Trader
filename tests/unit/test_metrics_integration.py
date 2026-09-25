from tests.support import BASE, DEPTH, TICK, TRADES, Harness, RawScript


def ready():
    sc=RawScript(); sc.bootstrap(); sc.seed_book(rows=10); sc.advance(600); sc.tick(); return sc


def test_engine_snapshot_exposes_metrics():
    sc=ready(); sc.advance(100); sc.depth(DEPTH,0,1,1,BASE,20); sc.trade(TRADES,BASE+TICK,3); sc.advance(100); sc.tick()
    snap=Harness().run(sc).engine.snapshot().instrument(1)
    assert snap.metrics.book.available
    assert snap.metrics.book.l1.bid_total == 20
    assert snap.metrics.book.l1.ask_total == 10
    assert snap.metrics.event_ofi == 10
    assert snap.metrics.trade_flow[0].buy_volume == 3
