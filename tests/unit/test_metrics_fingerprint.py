from hermes.replay import fingerprint as fp
from tests.support import Harness, RawScript


def test_metrics_bounded_state_is_covered_by_fingerprint():
    sc = RawScript()
    sc.bootstrap()
    sc.seed_book(rows=10)
    sc.advance(600)
    sc.tick()

    h = Harness().run(sc)
    inst = h.engine.instruments[1]

    before = fp.state_hash(h.engine)
    inst.metrics.advance(h.engine.last_mono_ns + 1)
    after = fp.state_hash(h.engine)

    assert after != before
