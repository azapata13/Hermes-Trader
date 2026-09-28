"""C9c candidate lifecycle: identity, TTL, entry drift, structural and safety invalidation, payload."""

from __future__ import annotations

import dataclasses

import pytest

from hermes.config import ConfigError, DecisionConfig, config_from_mapping
from hermes.decision.approval import approval_payload
from hermes.decision.candidate import CandidateEngine, Direction
from hermes.decision.driver import CandidateDriver
from hermes.decision.lifecycle import CandidateStatus, LifecycleTracker, TERMINAL, setup_id
from hermes.market.events import BookSide
from tests.support import DEPTH, TICK, TRADES, Harness, write_hrec
from tests.unit.test_candidate import PRE_RTH, S, T0, shift_book, trend


class Live:
    """Harness + driver fed event by event (what the live pipeline / replay observers do)."""

    def __init__(self, sc, cfg: DecisionConfig | None = None):
        self.sc = sc
        self.h = Harness()
        self.drv = CandidateDriver(self.h.engine, CandidateEngine(cfg))
        self.n = 0
        self.pump()

    def pump(self):
        for raw in self.sc.events[self.n:]:
            self.h.feed([raw])
            self.drv.after_event(raw, (), 0)
        self.n = len(self.sc.events)
        return self

    @property
    def lc(self) -> LifecycleTracker:
        return self.drv.lifecycle

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


def started(cfg=None) -> tuple[Live, object]:
    """Trend until the first ACTIONABLE LONG (trigger bar end T0+600, evaluated at T0+601)."""
    lv = Live(trend(+1, minutes=10), cfg)
    rec = lv.lc.latest()
    assert rec.status is CandidateStatus.ACTIONABLE and rec.candidate.direction is Direction.LONG
    assert rec.candidate.trigger_bar_end_s == T0 + 600
    return lv, rec


def price(units: int) -> float:
    return units * TICK


# ============================================================================ identity / initial status

def test_setup_id_is_deterministic_and_clock_free():
    lv1, r1 = started()
    lv2, r2 = started()
    assert r1.setup_id == r2.setup_id and r1.setup_id.startswith("S") and len(r1.setup_id) == 24
    moved = dataclasses.replace(r1.candidate, wall_ns=r1.candidate.wall_ns + 123_456_789, seq=999_999)
    assert setup_id(moved) == r1.setup_id                                  # no wall clock, no seq, no randomness
    other = dataclasses.replace(r1.candidate, trigger_bar_end_s=r1.candidate.trigger_bar_end_s + 30)
    assert setup_id(other) != r1.setup_id
    ids = [r.setup_id for r in lv1.lc.records]
    assert len(ids) == len(set(ids))                                      # one identity per decision point


def test_initial_statuses_none_blocked_actionable():
    lv, rec = started()
    early = [r for r in lv.lc.records if r.candidate.trigger_bar_end_s - T0 < 600]
    assert early and all(r.status is CandidateStatus.NONE for r in early)  # no setup: NONE, not BLOCKED
    assert all(r.reasons for r in early) and all(":" in r.reasons[0].code or r.reasons[0].code for r in early)
    pre = Live(trend(+1, t0=PRE_RTH, minutes=10))
    # outside the authorized window at CREATION: NONE (no candidate here), not BLOCKED
    assert pre.lc.records and all(r.status is CandidateStatus.NONE for r in pre.lc.records)
    assert any(x.code == "safety:outside_authorized_entry_hours" for x in pre.lc.records[-1].reasons)


# ============================================================================ expiry

def test_expires_at_trigger_end_plus_ttl_in_event_time():
    lv, rec = started()
    assert rec.expires_at_ns == (T0 + 630) * S
    lv.tick_until(629)
    assert lv.lc.get(rec.setup_id).status is CandidateStatus.ACTIONABLE
    lv.tick_until(630)
    r = lv.lc.get(rec.setup_id)
    assert r.status is CandidateStatus.EXPIRED and not r.actionable and r.reasons[0].code == "expired"
    assert r.status_wall_ns == (T0 + 630) * S and r.candidate is rec.candidate
    assert rec.setup_id not in lv.lc.active


def test_ttl_is_configurable():
    lv, rec = started(DecisionConfig(candidate_ttl_seconds=10))
    assert rec.expires_at_ns == (T0 + 610) * S
    lv.tick_until(610)
    assert lv.lc.get(rec.setup_id).status is CandidateStatus.EXPIRED


# ============================================================================ entry drift

def test_drift_beyond_eight_ticks_is_stale_and_never_chased():
    lv, rec = started()
    entry = rec.candidate.entry_reference
    bid = price(entry - 1)
    lv.at(605)
    shift_book(lv.sc, bid + 8 * TICK)                                     # ask exactly 8 ticks away: still valid
    lv.pump()
    assert lv.lc.get(rec.setup_id).status is CandidateStatus.ACTIONABLE
    lv.at(606)
    shift_book(lv.sc, bid + 9 * TICK)                                     # 9 ticks: STALE
    lv.pump()
    r = lv.lc.get(rec.setup_id)
    assert r.status is CandidateStatus.STALE and r.reasons[0].code == "entry_drift"
    assert r.candidate.entry_reference == entry and r.candidate.proposed_stop == rec.candidate.proposed_stop
    lv.at(607)
    shift_book(lv.sc, bid)                                                # price comes back: no revival
    lv.pump()
    assert lv.lc.get(rec.setup_id).status is CandidateStatus.STALE


def test_drift_threshold_is_configurable():
    lv, rec = started(DecisionConfig(max_entry_drift_ticks=2))
    lv.at(605)
    shift_book(lv.sc, price(rec.candidate.entry_reference - 1) + 3 * TICK)
    lv.pump()
    assert lv.lc.get(rec.setup_id).status is CandidateStatus.STALE


# ============================================================================ structural invalidation

def test_print_at_structural_invalidation_invalidates():
    lv, rec = started()
    lv.at(605)
    lv.sc.trade(TRADES, price(rec.candidate.structural_invalidation), 1)
    lv.pump()
    r = lv.lc.get(rec.setup_id)
    assert r.status is CandidateStatus.INVALIDATED and r.reasons[0].code == "structural_invalidation"
    assert r.candidate.structural_invalidation == rec.candidate.structural_invalidation   # never moved


def test_invalidation_dominates_drift_when_both_hold_at_one_check():
    lv, rec = started()
    lv.at(605)
    shift_book(lv.sc, price(rec.candidate.structural_invalidation))       # far down: drift AND structure
    lv.h.feed(lv.sc.events[lv.n:])                                         # engine only (no lifecycle check yet)
    lv.n = len(lv.sc.events)
    t = lv.lc.update(lv.h.engine, lv.sc.seq, lv.sc.wall)                   # ONE check sees both conditions
    assert t[0].to_status is CandidateStatus.INVALIDATED


def test_row_by_row_book_move_is_judged_only_on_coherent_snapshots():
    """IBKR depth arrives row by row. The intermediate crossed/unsorted snapshots are NOT price
    observations (temporary hold, no STALE/INVALIDATED from them); the first coherent snapshot at
    the new level is judged: it both drifts and touches the invalidation -> INVALIDATED."""
    lv, rec = started()
    lv.at(605)
    shift_book(lv.sc, price(rec.candidate.structural_invalidation))
    seen = []
    for raw in lv.sc.events[lv.n:]:
        lv.h.feed([raw])
        lv.drv.after_event(raw, (), 0)
        r = lv.lc.get(rec.setup_id)
        seen.append((r.status, r.approval_allowed_now, tuple(h.code for h in r.temporary_hold_reasons)))
    lv.n = len(lv.sc.events)
    assert CandidateStatus.STALE not in {x[0] for x in seen}
    holds = [x for x in seen if x[0] is CandidateStatus.ACTIONABLE and x[2]]
    assert holds and all(not allowed for _, allowed, _ in holds)
    assert {c for x in holds for c in x[2]} <= {"crossed_book_transition", "unsorted_book_transition"}
    final = lv.lc.get(rec.setup_id)
    assert final.status is CandidateStatus.INVALIDATED and not final.approval_allowed_now
    assert "best_bid" in final.reasons[0].detail


def test_transient_crossed_book_holds_approval_without_ending_the_candidate():
    """Regression: ACTIONABLE -> one transient crossed-but-VALID depth state -> no STALE/INVALIDATED,
    not approvable during it -> coherent VALID book returns -> same record approvable again."""
    lv, rec = started()
    assert lv.lc.get(rec.setup_id).approval_allowed_now
    entry = rec.candidate.entry_reference
    lv.at(605)
    lv.sc.depth(DEPTH, 0, 1, 1, price(entry + 1), 10)                      # best bid above best ask: crossed
    lv.pump()
    r = lv.lc.get(rec.setup_id)
    book = lv.h.engine.instruments[1].book
    assert book.state.value == "valid"                                     # authoritative state unchanged
    assert r.status is CandidateStatus.ACTIONABLE and not r.approval_allowed_now
    assert [h.code for h in r.temporary_hold_reasons] == ["crossed_book_transition"]
    assert r.transitions == () and r.candidate is rec.candidate
    p = approval_payload(r)
    assert p.actionable and not p.approval_allowed_now and p.temporary_hold_reasons
    lv.sc.depth(DEPTH, 0, 1, 1, price(entry - 1), 10)                      # coherent VALID book again
    lv.pump()
    r2 = lv.lc.get(rec.setup_id)
    assert r2.status is CandidateStatus.ACTIONABLE and r2.approval_allowed_now and not r2.temporary_hold_reasons
    assert r2.setup_id == rec.setup_id and r2.transitions == ()
    lv.tick_until(630)
    assert lv.lc.get(rec.setup_id).status is CandidateStatus.EXPIRED      # TTL still applies normally


def test_unsorted_transient_is_also_a_hold():
    lv, rec = started()
    entry = rec.candidate.entry_reference
    lv.at(605)
    lv.sc.depth(DEPTH, 1, 1, 1, price(entry - 1), 10)                      # bid row 1 == row 0 price: unsorted
    lv.pump()
    r = lv.lc.get(rec.setup_id)
    assert r.status is CandidateStatus.ACTIONABLE and not r.approval_allowed_now
    assert [h.code for h in r.temporary_hold_reasons] == ["unsorted_book_transition"]


def test_eligible_print_invalidates_even_during_a_hold():
    lv, rec = started()
    entry = rec.candidate.entry_reference
    lv.at(605)
    lv.sc.depth(DEPTH, 0, 1, 1, price(entry + 1), 10)                      # crossed (hold)
    lv.sc.trade(TRADES, price(rec.candidate.structural_invalidation), 1)  # an eligible print touches the level
    lv.pump()
    assert lv.lc.get(rec.setup_id).status is CandidateStatus.INVALIDATED


def test_ineligible_print_does_not_invalidate():
    lv, rec = started()
    lv.at(605)
    lv.sc.trade(TRADES, price(rec.candidate.structural_invalidation), 1, special="Z")   # ineligible
    lv.pump()
    assert lv.lc.get(rec.setup_id).status is CandidateStatus.ACTIONABLE


def test_suspect_book_is_a_final_block_not_a_hold():
    lv, rec = started()
    entry = rec.candidate.entry_reference
    lv.at(605)
    lv.sc.depth(DEPTH, 0, 1, 1, price(entry + 1), 10)                      # crossed ...
    lv.tick_until(606)                                                     # ... beyond grace -> SUSPECT
    r = lv.lc.get(rec.setup_id)
    assert r.status is CandidateStatus.BLOCKED and not r.approval_allowed_now
    assert any(x.code == "book_not_valid" for x in r.reasons)
    lv.sc.depth(DEPTH, 0, 1, 1, price(entry - 1), 10)                      # book recovers: no revival
    lv.tick_until(608)
    assert lv.lc.get(rec.setup_id).status is CandidateStatus.BLOCKED


# ============================================================================ safety invalidation

def test_connection_loss_blocks_actionable_candidate():
    lv, rec = started()
    lv.at(605)
    lv.sc.error(-1, 1100)
    lv.pump()
    r = lv.lc.get(rec.setup_id)
    assert r.status is CandidateStatus.BLOCKED
    codes = {x.code for x in r.reasons}
    assert "connection_unusable" in codes and "book_not_valid" in codes


def test_depth_reset_changes_continuity_and_blocks():
    lv, rec = started()
    lv.at(605)
    lv.sc.error(DEPTH, 317, "Market depth data has been RESET")
    lv.pump()
    r = lv.lc.get(rec.setup_id)
    assert r.status is CandidateStatus.BLOCKED
    assert any(x.code == "continuity_epoch_changed" and "book_epoch" in x.detail for x in r.reasons)


def test_trades_resubscription_changes_generation_and_blocks():
    lv, rec = started()
    lv.at(605)
    lv.sc.request("reqTickByTickData", 30_003, tick_type="AllLast")
    lv.pump()
    r = lv.lc.get(rec.setup_id)
    assert r.status is CandidateStatus.BLOCKED and any(x.code == "stream_generation_changed" for x in r.reasons)


def test_leaving_rth_blocks():
    lv, rec = started()
    lv.h.engine.instruments[1].sessions.in_rth = False                    # as at the RTH close boundary
    t = lv.lc.update(lv.h.engine, 10**9, (T0 + 605) * S)
    assert t and t[0].to_status is CandidateStatus.BLOCKED
    assert any(x.code == "outside_authorized_entry_hours" for x in lv.lc.get(rec.setup_id).reasons)


_UNAVAILABLE = {
    "c7_metrics_unavailable": lambda inst: (setattr(inst.metrics, "_book_valid", False),
                                            setattr(inst.metrics, "_book_reason", "injected")),
    "c8_structure_unavailable": lambda inst: (setattr(inst.structure, "_available", False),
                                              setattr(inst.structure, "_reason", "injected")),
    "c8_patterns_unavailable": lambda inst: (setattr(inst.patterns, "_available", False),
                                             setattr(inst.patterns, "_reason", "injected")),
}


@pytest.mark.parametrize("code", sorted(_UNAVAILABLE))
def test_c7_c8_evidence_loss_without_epoch_change_blocks(code):
    """Regression (C9d review): ACTIONABLE -> required C7/C8 evidence becomes unavailable while every
    continuity epoch and stream generation stays identical -> BLOCKED, never approval-eligible."""
    from hermes.decision.approval import approval_payload
    from hermes.decision.safety import approval_check, facts_from_engine
    lv, rec = started()
    eng, inst = lv.h.engine, lv.h.engine.instruments[1]
    before = facts_from_engine(eng, inst, (T0 + 605) * S)
    _UNAVAILABLE[code](inst)                                              # availability lost, NO epoch bump
    after = facts_from_engine(eng, inst, (T0 + 605) * S)
    assert after.continuity == before.continuity and after.streams == before.streams
    safety = approval_check(eng, lv.lc.get(rec.setup_id), now_wall_ns=(T0 + 605) * S)
    assert code in safety.hard_codes and not safety.allowed               # approval-time check sees it too
    assert not approval_payload(lv.lc.get(rec.setup_id), safety).approval_allowed_now
    t = lv.lc.update(eng, 10**9, (T0 + 605) * S)
    r = lv.lc.get(rec.setup_id)
    assert t and t[0].to_status is CandidateStatus.BLOCKED and r.status is CandidateStatus.BLOCKED
    assert [x.code for x in r.reasons] == [code]                          # not an epoch-change reason
    assert not r.approval_allowed_now and not approval_payload(r).approval_allowed_now


def test_c7_metrics_loss_on_real_book_path_blocks_even_though_metrics_epoch_is_unchanged():
    """Real engine path: a VALID book with an emptied bid side invalidates C7 book metrics WITHOUT
    bumping the C7 metrics epoch; the current-availability rule catches it on its own."""
    lv, rec = started()
    inst = lv.h.engine.instruments[1]
    m_epoch = inst.metrics.continuity_epoch
    lv.at(605)
    for _ in range(len(inst.book.levels(BookSide.BID))):
        lv.sc.depth(DEPTH, 0, 2, 1, 0.0, 0)                               # delete bid rows (row 0 each time)
    lv.pump()
    r = lv.lc.get(rec.setup_id)
    assert inst.metrics.continuity_epoch == m_epoch and not inst.metrics.token()[1]
    assert r.status is CandidateStatus.BLOCKED and not r.approval_allowed_now
    assert "c7_metrics_unavailable" in {x.code for x in r.reasons}


def test_safety_dominates_everything_and_annotations_never_reactivate():
    lv, rec = started()
    lv.at(605)
    lv.sc.trade(TRADES, price(rec.candidate.structural_invalidation), 1)  # structural hit ...
    lv.sc.error(-1, 1100)                                                 # ... and connection loss
    lv.pump()
    r = lv.lc.get(rec.setup_id)
    assert r.status is CandidateStatus.INVALIDATED                        # first event decided it
    lv2, rec2 = started()
    lv2.at(605)
    lv2.sc.error(-1, 1100)
    lv2.pump()
    blocked = lv2.lc.get(rec2.setup_id)
    assert blocked.status is CandidateStatus.BLOCKED
    note = lv2.lc.annotate(rec2.setup_id, "memory: 9/10 similar setups worked")   # informational only
    assert note.status is CandidateStatus.BLOCKED and note.annotations and not note.actionable
    lv2.tick_until(620)
    assert lv2.lc.get(rec2.setup_id).status is CandidateStatus.BLOCKED
    assert all(s in TERMINAL for s in (CandidateStatus.BLOCKED, CandidateStatus.EXPIRED, CandidateStatus.STALE))


# ============================================================================ approval payload

def test_approval_payload_is_complete_and_follows_status():
    lv, rec = started()
    p = approval_payload(lv.lc.get(rec.setup_id))
    c = rec.candidate
    assert p.actionable and p.status == "ACTIONABLE" and p.mode == "HUMAN_APPROVAL" and p.direction == "LONG"
    assert (p.entry_reference, p.proposed_stop, p.structural_invalidation) == (
        c.entry_reference, c.proposed_stop, c.structural_invalidation)
    assert p.entry_side == "best_ask" and p.structure_source == "recent_1m_window_low"
    assert p.risk_points == c.risk_points and p.risk_usd_per_contract == pytest.approx(c.risk_points * 2)
    assert p.expires_at_ns == (T0 + 630) * S and p.evaluation_lag_ms == c.evaluation_lag_ms
    assert p.regime_5m.result == "LONG" and p.setup_1m.bars_used == 3 and p.trigger_30s.window_end_s == T0 + 600
    assert p.trigger_30s.unknown_volume >= 0 and p.orderflow_evidence == c.orderflow_evidence
    assert any("MBP" in n for n in p.notes) and p.screenshot_ref is None
    lv.tick_until(630)
    p2 = approval_payload(lv.lc.get(rec.setup_id))
    assert p2.status == "EXPIRED" and not p2.actionable and p2.setup_id == p.setup_id
    assert p2.entry_reference == p.entry_reference                        # the proposal itself never changes


# ============================================================================ determinism

def test_lifecycle_replay_is_deterministic(tmp_path):
    from hermes.replay.runner import ReplayOptions, replay_session
    sc = trend(+1, minutes=12)
    d = write_hrec(tmp_path / "s", sc.events)
    opts = ReplayOptions(observers=(lambda eng: CandidateDriver(eng, CandidateEngine()),))
    a, b = replay_session(d, opts), replay_session(d, opts)
    live = Live(sc)
    da, db = a.observers[0], b.observers[0]
    assert da.fingerprint() == db.fingerprint() == live.drv.fingerprint()
    assert tuple(da.lifecycle.transitions) == tuple(live.lc.transitions) and da.lifecycle.transitions
    assert a.final_hash == replay_session(d).final_hash                    # market hash untouched


def test_lifecycle_config_validation():
    with pytest.raises(ConfigError):
        config_from_mapping({"decision": {"candidate_ttl_seconds": 0}})
    with pytest.raises(ConfigError):
        config_from_mapping({"decision": {"max_entry_drift_ticks": -1}})
    d = config_from_mapping({}).decision
    assert (d.candidate_ttl_seconds, d.max_entry_drift_ticks) == (30, 8)
