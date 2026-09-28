"""C9f DecisionRuntime: evaluation cadence, lifecycle cost gating, journal / checkpoint determinism."""

from __future__ import annotations

import pytest

from hermes.decision.lifecycle import LifecycleTracker
from hermes.decision.runtime import DecisionRuntime, JournalKind, JournalRecord
from tests.support import Harness
from tests.unit.test_candidate import trend


def drive(sc, rt_factory=DecisionRuntime):
    h = Harness()
    rt = rt_factory(h.engine)
    bars_changed = []
    for raw in sc.events:
        before = next(iter(h.engine.instruments.values())).bars.completed[30] if h.engine.instruments else 0
        h.feed([raw])
        after = next(iter(h.engine.instruments.values())).bars.completed[30] if h.engine.instruments else 0
        n = len(rt.journal)
        rt.after_event(raw, (), raw.recv_mono_ns)
        evaluated = any(r.kind is JournalKind.DECISION_EVALUATED for r in rt.journal[n:])
        bars_changed.append((raw.seq, after > before, evaluated))
    rt.finalize()
    return h, rt, bars_changed


@pytest.fixture(scope="module")
def run12():
    return drive(trend(+1, minutes=12))


def test_one_evaluation_per_completed_30s_bar_and_none_on_ordinary_events(run12):
    _, rt, rows = run12
    for seq, bar_done, evaluated in rows:
        assert evaluated == bar_done, seq                 # evaluation iff a 30 s bar completed on this event
    assert sum(e for _, _, e in rows) == rt.driver.candidates.stats.evaluations > 20
    assert sum(1 for _, b, _ in rows if not b) > 10 * rt.driver.candidates.stats.evaluations


def test_lifecycle_runs_only_while_a_candidate_is_actionable(monkeypatch):
    calls = []
    orig = LifecycleTracker.update

    def counted(self, engine, seq, wall_ns):
        calls.append((seq, bool(self.active)))
        return orig(self, engine, seq, wall_ns)

    monkeypatch.setattr(LifecycleTracker, "update", counted)
    _, rt, rows = drive(trend(+1, minutes=12))
    assert calls and all(active for _, active in calls)                  # never called with nothing active
    j = rt.journal
    terminal = {"CANDIDATE_BLOCKED", "CANDIDATE_STALE", "CANDIDATE_INVALIDATED", "CANDIDATE_EXPIRED"}
    spans = []
    for r in j:
        if r.kind is JournalKind.CANDIDATE_ACTIONABLE:
            end = next((x.seq for x in j if x.setup_id == r.setup_id and x.kind.value in terminal), 10**12)
            spans.append((r.seq, end))
    assert spans
    expected = {s for s, _, _ in rows if any(a < s <= e for a, e in spans)}
    assert {s for s, _ in calls} == expected


def test_journal_and_checkpoints_are_deterministic(run12):
    _, a, _ = run12
    _, b, _ = drive(trend(+1, minutes=12))
    assert [r.row() for r in a.journal] == [r.row() for r in b.journal]
    assert [c.row() for c in a.checkpoints] == [c.row() for c in b.checkpoints]
    assert a.final == b.final and a.fingerprint() == b.fingerprint()
    assert [JournalRecord.from_row(r.row()) for r in a.journal] == a.journal     # lossless row round trip


def test_journal_is_compact_and_checkpoints_follow_journal_events(run12):
    _, rt, rows = run12
    assert len(rt.journal) < len(rows) / 20                               # transitions, not ticks
    assert [c.seq for c in rt.checkpoints] == sorted({r.seq for r in rt.journal})
    for r in rt.journal:
        assert len(r.row()) == 12 and all(len(c) == 3 for c in r.reasons)
    act = [r for r in rt.journal if r.kind is JournalKind.CANDIDATE_ACTIONABLE]
    assert act and all(r.proposal_id.startswith("P") for r in act)


def test_decision_fingerprint_changes_only_with_decision_events(run12):
    _, rt, _ = run12
    fps = [c.fingerprint for c in rt.checkpoints]
    assert len(set(fps)) == len(fps)                                      # every decision event moves it
    assert rt.final.fingerprint == fps[-1]                                # nothing after the last event


def test_on_record_hook_is_read_only():
    seen = []
    _, rt, _ = drive(trend(+1, minutes=11), lambda eng: DecisionRuntime(eng, on_record=lambda r, v: seen.append((r, v))))
    _, plain, _ = drive(trend(+1, minutes=11))
    assert [r for r, _ in seen] == rt.journal and rt.final == plain.final
    assert any(v is not None for r, v in seen if r.kind is JournalKind.APPROVAL_VIEW_CREATED)
