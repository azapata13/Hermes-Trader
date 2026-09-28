"""C9f snapshot optimization: exact rolling sums are behavior-identical to the original rescans."""

from __future__ import annotations

import random

import pytest

from hermes.market.classify import Aggressor
from hermes.market.metrics import MetricsEngine
from hermes.market.rolling import WindowSums
from hermes.market.structure import StructureEngine
from tests.support import Harness
from tests.unit.test_candidate import trend

S = 1_000_000_000


def _busy_events(seconds: float = 75, seed: int = 3):
    import tempfile
    from pathlib import Path

    from hermes.storage.reader import iter_raw_events
    from tools.c9_benchmark import synthetic_session
    d = synthetic_session(seconds / 60, Path(tempfile.mkdtemp()) / "s", seed=seed)
    return list(iter_raw_events(d))


@pytest.fixture(scope="module")
def busy():
    return _busy_events()


def _check_every_event(events, stride: int = 1):
    h = Harness()
    n = 0
    for k, raw in enumerate(events):
        h.feed([raw])
        if k % stride or not h.engine.instruments:
            continue
        inst = next(iter(h.engine.instruments.values()))
        assert inst.metrics.snapshot() == inst.metrics.snapshot_reference(), raw.seq
        assert inst.structure.snapshot() == inst.structure.snapshot_reference(), raw.seq
        n += 1
    return h, n


def test_busy_synthetic_market_identical_at_every_event(busy):
    h, n = _check_every_event(busy, stride=3)
    inst = next(iter(h.engine.instruments.values()))
    assert n > 5000
    m, st = inst.metrics, inst.structure
    assert all(r.exact for r in (m._r_depth, m._r_ofi, m._r_trades, m._r_bbo, st._r_liq, st._r_hit))
    assert len(m._r_depth) < 2 * 2048 + 2 * m.snapshot().velocity[2].depth_updates  # bounded (compaction)
    w30 = inst.metrics.snapshot().velocity[2]
    assert w30.depth_updates > 1000 and w30.trades > 50           # the windows really are populated


@pytest.mark.parametrize("direction", [+1, -1])
def test_trend_scenarios_identical_at_every_event(direction):
    _check_every_event(trend(direction, minutes=12).events)


def test_window_sums_fall_back_when_time_goes_backwards():
    r = WindowSums((5 * S,), 1)
    r.push(10 * S, (1,), 10 * S)
    r.push(9 * S, (1,), 10 * S)                                  # out of order
    assert r.sums_at(0, 12 * S) is None and not r.exact
    r.clear()
    r.push(10 * S, (1,), 10 * S)
    assert r.sums_at(0, 20 * S) == (0,)                          # cutoff 15 s applied
    r.push(14 * S, (1,), 20 * S)                                 # older than an applied cutoff
    assert r.sums_at(0, 20 * S) is None
    r.clear()
    r.push(10 * S, (2,), 10 * S)
    assert r.sums_at(0, 12 * S) == (2,)
    assert r.sums_at(0, 11 * S) is None                          # earlier "now": never trusted


def test_window_sums_multi_window_and_compaction():
    r = WindowSums((1 * S, 5 * S, 30 * S), 2)
    ref = []
    t = 0
    for k in range(20000):
        t += 3_000_000                                           # 333 entries / s
        ref.append((t, k % 7, 1))
        r.push(t, (k % 7, 1), t)
        if k % 1000 == 999:
            for j, w in enumerate((1, 5, 30)):
                cut = t - w * S
                want = (sum(v for x, v, _ in ref if x >= cut), sum(1 for x, _, _ in ref if x >= cut))
                assert r.sums_at(j, t) == want
    assert len(r) < 2 * 30 * 334 + 2 * 2048                      # older entries compacted away


class _T:
    def __init__(self, t, size, side, price):
        self.recv_mono_ns, self.size, self.aggressor, self.price_units = t, size, side, price
        self.eligible = True


def test_metrics_out_of_order_input_falls_back_and_stays_identical():
    rnd = random.Random(11)
    m = MetricsEngine(1)
    t = 100 * S
    for k in range(3000):
        t += rnd.randint(-2 * S, 3 * S) if k % 97 == 0 else rnd.randint(0, 50_000_000)   # rare jumps back
        side = rnd.choice((Aggressor.BUY, Aggressor.SELL, Aggressor.UNKNOWN))
        m.on_trade(_T(t, rnd.randint(1, 4), side, 84000 + rnd.randint(-8, 8)))
        if k % 5 == 0:
            m.on_bbo(t)
        if k % 50 == 0:
            m.advance(t + rnd.randint(0, S))
        assert m.snapshot() == m.snapshot_reference(), k
        if k == 1500:
            m.break_trade("test", t)
            m.break_all("test", t)


def test_structure_break_clears_the_cache(busy):
    h = Harness()
    for raw in busy[:4000]:
        h.feed([raw])
    st: StructureEngine = next(iter(h.engine.instruments.values())).structure
    st.break_book("test", st._now_ns)
    assert all(len(r) == 0 and r.exact for r in (st._r_liq, st._r_hit))
    assert st.snapshot() == st.snapshot_reference()


def test_decision_journal_identical_with_reference_snapshots(monkeypatch):
    """Same candidate decisions / journal / fingerprints whether C7/C8 snapshots use the rolling
    caches or the original rescans."""
    from hermes.decision.runtime import DecisionRuntime

    def run():
        h = Harness()
        rt = DecisionRuntime(h.engine)
        for raw in trend(+1, minutes=12).events:
            h.feed([raw])
            rt.after_event(raw, (), raw.recv_mono_ns)
        rt.finalize()
        return rt

    fast = run()
    monkeypatch.setattr(MetricsEngine, "snapshot", MetricsEngine.snapshot_reference)
    monkeypatch.setattr(StructureEngine, "snapshot", StructureEngine.snapshot_reference)
    ref = run()
    assert [r.row() for r in fast.journal] == [r.row() for r in ref.journal] and fast.final == ref.final
    assert any(r.kind.value == "CANDIDATE_ACTIONABLE" for r in fast.journal)
