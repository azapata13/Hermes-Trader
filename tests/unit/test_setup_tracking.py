"""V1 Pro D3/D4/D6: setup lifecycle tracking, MFE/MAE outcome and re-arm (analytics only).

Synthetic, deterministic market scripts drive the REAL decision runtime (same code path as live and
replay). Prices are in grid units (MNQ: 4 units per point, tick 0.25).
"""

from __future__ import annotations

import dataclasses

from hermes.decision.candidate import Direction
from hermes.decision.lifecycle import CandidateStatus
from hermes.decision.runtime import DecisionRuntime
from hermes.decision.tracking import (
    OutcomeStatus,
    SetupPhase,
    SetupTracker,
    TrackingConfig,
)
from tests.support import TICK, TRADES, Harness
from tests.unit.test_candidate import T0, S, shift_book, trend


class RT:
    """Harness + the real DecisionRuntime fed event by event."""

    def __init__(self, sc, tracking: TrackingConfig | None = None):
        self.sc = sc
        self.h = Harness()
        self.events = []
        self.rt = DecisionRuntime(self.h.engine, tracking=tracking,
                                  on_setup_event=lambda e, v: self.events.append((e, v)))
        self.n = 0
        self.pump()

    def pump(self):
        for raw in self.sc.events[self.n:]:
            self.h.feed([raw])
            self.rt.after_event(raw, (), 0)
        self.n = len(self.sc.events)
        return self

    def at(self, t_rel: float):
        self.sc.at(T0 + t_rel)
        return self

    def tick_until(self, t_rel: float):
        t = self.sc.wall / S - T0
        while t < t_rel:
            t = min(t_rel, int(t) + 1)
            self.sc.at(T0 + t)
            self.sc.tick()
        return self.pump()

    def print_at(self, t_rel: float, units: int, size: int = 1):
        self.at(t_rel)
        self.sc.trade(TRADES, units * TICK, size)
        return self.pump()

    @property
    def tracker(self) -> SetupTracker:
        return self.rt.setups


def started(tracking=None) -> tuple[RT, object]:
    r = RT(trend(+1, minutes=10), tracking)
    rec = r.rt.driver.lifecycle.latest()
    assert rec.status is CandidateStatus.ACTIONABLE and rec.candidate.direction is Direction.LONG
    return r, rec


def view(r: RT, rec):
    return r.tracker.get(rec.setup_id)


# ============================================================================ D3 lifecycle

def test_actionable_candidate_is_tracked_active_with_its_original_facts():
    r, rec = started()
    v = view(r, rec)
    c = rec.candidate
    assert v.phase is SetupPhase.ACTIVE and v.outcome.status is OutcomeStatus.OPEN
    assert (v.original_entry_units, v.original_stop_units) == (c.entry_reference, c.proposed_stop)
    assert v.risk_units == abs(c.entry_reference - c.proposed_stop) > 0
    assert v.created_wall_ns == rec.created_wall_ns and v.expires_at_ns == rec.expires_at_ns
    assert v.proposal_id == r.rt.proposals[rec.setup_id]
    assert dict(v.features)["direction"] == "LONG" and dict(v.features)["tape"] == "with"
    assert r.events[0][0].kind == "TRACKED"


def test_expired_window_becomes_monitoring_not_dead():
    r, rec = started()
    r.tick_until(630)                                       # trigger end 600 + TTL 30 s
    assert r.rt.driver.lifecycle.get(rec.setup_id).status is CandidateStatus.EXPIRED
    v = view(r, rec)
    assert v.phase is SetupPhase.MONITORING and v.window_close_status == "EXPIRED"
    assert v.monitoring_start_wall_ns == v.window_closed_wall_ns == (T0 + 630) * S
    assert v.outcome.status is OutcomeStatus.OPEN and v.outcome.mfe_after_close_units == 0


def test_stale_window_becomes_monitoring_and_keeps_the_original_entry():
    r, rec = started()
    e = rec.candidate.entry_reference
    r.at(606)
    shift_book(r.sc, (e - 1 + 9) * TICK)                    # 9 ticks of drift -> C9 STALE
    r.pump()
    v = view(r, rec)
    assert r.rt.driver.lifecycle.get(rec.setup_id).status is CandidateStatus.STALE
    assert v.phase is SetupPhase.MONITORING and v.window_close_status == "STALE"
    assert v.window_close_reasons == ("entry_drift",)
    assert v.original_entry_units == e                      # never moved
    r.print_at(607, e + 20)
    v = view(r, rec)
    assert v.outcome.mfe_after_close_units == 20 and v.outcome.mfe_units == 20


def test_c9_structural_invalidation_marks_the_setup_invalidated():
    r, rec = started()
    r.print_at(605, rec.candidate.structural_invalidation)
    v = view(r, rec)
    assert v.phase is SetupPhase.INVALIDATED and v.invalidation_reason in ("structural_invalidation", "stop_touched")


def test_transitions_are_audited_in_order_and_deterministic():
    def run():
        r, rec = started()
        r.print_at(605, rec.candidate.entry_reference + 8)
        r.tick_until(630)
        r.print_at(640, rec.candidate.entry_reference - 4)
        r.rt.finalize()
        return [e.row() for e in r.tracker.events], r.tracker.fingerprint()
    (rows_a, fp_a), (rows_b, fp_b) = run(), run()
    assert rows_a == rows_b and fp_a == fp_b
    kinds = [row[3] for row in rows_a]
    assert kinds[:2] == ["TRACKED", "WINDOW_CLOSED"] and kinds[-1] == "OUTCOME_FINAL"


def test_duplicate_and_out_of_order_raw_events_are_applied_once():
    r, rec = started()
    r.print_at(605, rec.candidate.entry_reference + 8)
    raw = r.sc.events[-1]
    before = view(r, rec)
    r.tracker.after_event(r.h.engine, raw)                  # duplicate
    r.tracker.after_event(r.h.engine, r.sc.events[-5])      # out of order
    assert view(r, rec) == before and before.outcome.prints >= 1


def test_tracking_never_changes_c9_lifecycle_or_decision_journal():
    plain = Harness()
    rt0 = DecisionRuntime(plain.engine)
    sc = trend(+1, minutes=11)
    for raw in sc.events:
        plain.feed([raw])
        rt0.after_event(raw, (), 0)
    r = RT(trend(+1, minutes=11))
    assert [j.row() for j in r.rt.journal] == [j.row() for j in rt0.journal]
    assert r.rt.fingerprint() == rt0.fingerprint()          # C9 decision fingerprint unaffected


# ============================================================================ D4 outcome (MFE / MAE)

def test_long_mfe_mae_points_dollars_and_first_times():
    r, rec = started()
    e = rec.candidate.entry_reference
    r.print_at(605, e + 8)        # +2 pt
    r.print_at(606, e - 4)        # -1 pt
    r.print_at(607, e + 12)       # +3 pt
    r.print_at(608, e + 12)       # equal high: the FIRST time is kept
    o = view(r, rec).outcome
    v = view(r, rec)
    assert (o.mfe_units, o.mae_units) == (12, 4)
    assert o.mfe_wall_ns == (T0 + 607) * S and o.mae_wall_ns == (T0 + 606) * S
    assert v.points(o.mfe_units) == 3.0 and v.usd(o.mfe_units) == 6.0          # MNQ $2/pt
    assert v.points(o.mae_units) == 1.0 and v.usd(o.mae_units) == 2.0


def test_r_multiples_first_touch_and_stop_first_freezes_excursions():
    r, rec = started()
    e, s = rec.candidate.entry_reference, rec.candidate.proposed_stop
    risk = e - s
    r.print_at(605, e + risk)                               # +1R
    r.print_at(606, e + 2 * risk - 1)                       # just below +2R
    o = view(r, rec).outcome
    assert o.r_wall_ns[0] == (T0 + 605) * S and o.r_wall_ns[1] is None
    r.print_at(607, s)                                      # stop touch
    v = view(r, rec)
    assert v.outcome.status is OutcomeStatus.STOPPED and v.outcome.stop_touched
    assert v.phase is SetupPhase.INVALIDATED and v.finished
    frozen = v.outcome
    r.print_at(608, e + 10 * risk)                          # after the stop: never applied
    assert view(r, rec).outcome == frozen


def test_immediate_adverse_move_to_stop_is_stop_first():
    r, rec = started()
    r.print_at(605, rec.candidate.proposed_stop - 3)        # gap through the stop
    o = view(r, rec).outcome
    assert o.status is OutcomeStatus.STOPPED and o.r_wall_ns[0] is None
    assert o.mae_units == rec.candidate.entry_reference - rec.candidate.proposed_stop + 3


def test_horizon_results_and_completion():
    r, rec = started(TrackingConfig(horizons_s=(60, 180)))
    e = rec.candidate.entry_reference
    r.print_at(650, e + 4)                                  # before 60 s after creation (601 + 60 = 661)
    r.print_at(662, e + 6)                                  # first print at/after +60 s
    r.print_at(700, e + 9)
    r.tick_until(600 + 1 + 180)                             # horizon reached
    v = view(r, rec)
    assert v.outcome.horizon_units[0] == 6 and v.outcome.horizon_units[1] is None   # no print at/after +180
    assert v.outcome.status is OutcomeStatus.COMPLETE and v.phase is SetupPhase.ENDED


def test_tape_continuity_break_makes_the_outcome_incomplete_never_interpolated():
    r, rec = started()
    r.print_at(605, rec.candidate.entry_reference + 8)
    r.at(606)
    r.sc.request("reqTickByTickData", 20_003, tick_type="AllLast")   # trades resubscribed: new tape epoch
    r.pump()
    v = view(r, rec)
    assert v.outcome.status is OutcomeStatus.INCOMPLETE
    assert v.outcome.incomplete_reason == "tape_continuity_broken" and v.outcome.mfe_units == 8


def test_session_end_finalizes_open_setups_as_incomplete():
    r, rec = started()
    r.rt.finalize()
    assert view(r, rec).outcome.status is OutcomeStatus.INCOMPLETE
    assert view(r, rec).outcome.incomplete_reason == "session_ended"


def test_short_excursions_are_mirrored():
    r = RT(trend(-1, minutes=10))
    rec = r.rt.driver.lifecycle.latest()
    assert rec.candidate.direction is Direction.SHORT
    e = rec.candidate.entry_reference
    r.print_at(605, e - 10)                                 # favorable for a SHORT
    r.print_at(606, e + 3)
    o = r.tracker.get(rec.setup_id).outcome
    assert (o.mfe_units, o.mae_units) == (10, 3)


# ============================================================================ D6 re-arm

def test_rearm_requires_a_new_actionable_candidate_with_fresh_facts():
    r = RT(trend(+1, minutes=10.55))                        # 601 ACTIONABLE, 630 EXPIRED, 631 new ACTIONABLE
    lc = r.rt.driver.lifecycle
    first = next(x for x in lc.records if x.status is not CandidateStatus.NONE)
    old = r.tracker.get(first.setup_id)
    assert old.phase is SetupPhase.REARMED and old.rearmed_by is not None
    second = lc.get(old.rearmed_by)                        # created at the next 30 s decision point
    assert second.setup_id != first.setup_id and second.created_wall_ns == (T0 + 631) * S
    new = r.tracker.get(second.setup_id)
    assert new.rearm_of == first.setup_id and new.phase is SetupPhase.ACTIVE
    assert old.rearm_entry_units == second.candidate.entry_reference == new.original_entry_units
    assert old.original_entry_units == first.candidate.entry_reference               # old entry untouched
    assert new.created_wall_ns > old.window_closed_wall_ns                           # new timestamp
    assert new.original_stop_units == second.candidate.proposed_stop                  # new stop / risk
    assert r.rt.proposals[second.setup_id] != r.rt.proposals[first.setup_id]          # new proposal


def test_old_record_or_non_actionable_record_can_never_rearm():
    r = RT(trend(+1, minutes=11))
    lc = r.rt.driver.lifecycle
    first = next(x for x in lc.records if x.status is not CandidateStatus.NONE)
    n = len(r.tracker.events)
    raw = r.sc.events[-1]
    assert r.tracker.on_actionable(first, "P", raw) is None                          # already tracked / expired
    blocked = dataclasses.replace(lc.latest(), setup_id="S" + "x" * 23, status=CandidateStatus.BLOCKED)
    assert r.tracker.on_actionable(blocked, None, raw) is None
    assert len(r.tracker.events) == n


def test_an_invalidated_setup_is_never_rearmed():
    r, rec = started()
    r.print_at(605, rec.candidate.proposed_stop)             # stop touched -> INVALIDATED, finished
    r2 = view(r, rec)
    assert r2.phase is SetupPhase.INVALIDATED
    # same direction candidate later: no link to an invalidated setup
    assert r.tracker._rearm_candidate("LONG", (T0 + 640) * S) is None


def test_rearm_links_only_one_monitoring_setup_and_only_once():
    r = RT(trend(+1, minutes=12))
    links = [(v.setup_id, v.rearmed_by) for v in r.tracker.by_id.values() if v.rearmed_by]
    targets = [b for _, b in links]
    assert len(targets) == len(set(targets))                 # each new candidate re-arms one setup at most
    rearm_events = [e for e in r.tracker.events if e.kind == "REARMED"]
    assert len({e.setup_id for e in rearm_events}) == len(rearm_events)


def test_opposite_direction_candidate_does_not_rearm():
    t = SetupTracker()
    assert t._rearm_candidate("SHORT", 0) is None
