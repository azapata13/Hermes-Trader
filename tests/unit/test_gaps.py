"""V1 Pro gap analysis: 'what happened between two real signals?' — recorded data only, shadow
reconstruction clearly labelled, never presented as a Hermès signal or an execution."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from hermes.analytics.gaps import (
    RECONSTRUCTED_LABEL,
    EvalPoint,
    analyze_windows,
    excursion,
)
from tests.support import SPEC, write_hrec
from tests.unit.test_candidate import PRE_RTH, trend

S = 10**9
UPP = 4


def ev(t, sid, status="NONE", direction="NONE", stages=("NEUTRAL", "NONE", "NONE"), ok=True, blocks=()):
    return EvalPoint(t * S, sid, status, direction, stages[0], stages[1], stages[2], "below", "SHORT", "SHORT",
                     "NEUTRAL", ok, True, tuple(blocks))


def path(start, prices, step=10):
    return [((start + i * step) * S, round(p * UPP)) for i, p in enumerate(prices)]


# ---------------------------------------------------------------------------- pure analysis

def test_excursion_math_and_label():
    px = path(0, [100.0, 99.0, 101.0, 95.0, 80.0, 82.0])
    x = excursion(px, "SHORT")
    assert (x.mfe_units, x.mae_units) == (80, 4) and x.mfe_wall_ns == 40 * S and x.mae_wall_ns == 20 * S
    assert x.label == RECONSTRUCTED_LABEL and "not a Hermès signal" in x.label and "not executed" in x.label
    assert excursion([], "LONG") is None


def test_the_observed_case_20_points_short_between_two_long_signals():
    """Two real LONG signals; between them the market fell ~20 pt and every 30 s decision was blocked
    or neutral: reported as a move WITHOUT candidate, with the reasons, never as a signal."""
    evals = [ev(0, "SL1", "ACTIONABLE", "LONG", ("LONG", "LONG", "LONG"))]
    evals += [ev(30 * k, f"SN{k}", "BLOCKED" if k % 2 else "NONE", "NONE", ("SHORT", "SHORT", "NONE"),
                 blocks=("trigger_30s:agrees_with_1m_setup",) + (("safety:book_not_valid",) if k % 2 else ()))
              for k in range(1, 20)]
    evals.append(ev(600, "SL2", "ACTIONABLE", "LONG", ("LONG", "LONG", "LONG")))
    prices = [21000 - 20.25 * i / 59 for i in range(60)]
    w = analyze_windows(evals, path(0, prices), [(0, "SL1"), (600 * S, "SL2")], session_start=0,
                        session_end=700 * S, units_per_point=UPP, min_move_pt=20, health=[(0, True, ())])
    mid = next(x for x in w if x.opened_by == "SL1")
    assert (mid.opened_by, mid.closed_by, mid.opened_direction) == ("SL1", "SL2", "LONG")
    assert "MOVE_WITHOUT_CANDIDATE:SHORT" in mid.classification and "CANDIDATE_BLOCKED" in mid.classification
    assert mid.actionable == [] and mid.evaluations == 19
    assert mid.stages["regime_5m"] == {"SHORT": 19} and mid.stages["trigger_30s"] == {"NONE": 19}
    assert mid.shadow["SHORT"].mfe_units == 81 and "LONG" not in mid.shadow       # 20.25 pt, hindsight only
    assert any("trigger_30s:agrees_with_1m_setup" in r for r in mid.why_no_signal)
    assert any("safety:book_not_valid" in r for r in mid.why_no_signal)


def test_move_in_the_direction_of_the_opening_signal_is_not_a_missed_candidate():
    evals = [ev(0, "SS1", "ACTIONABLE", "SHORT", ("SHORT", "SHORT", "SHORT"))]
    w = analyze_windows(evals, path(0, [21000 - i for i in range(30)]), [(0, "SS1")], session_start=0,
                        session_end=400 * S, units_per_point=UPP, min_move_pt=20)
    x = next(x for x in w if x.opened_by == "SS1")
    assert len(w) == 1                                    # the empty [start, first signal) window is skipped
    assert "MOVE_WITH_OPENING_SIGNAL:SHORT" in x.classification
    assert not any(c.startswith("MOVE_WITHOUT_CANDIDATE") for c in x.classification)


def test_small_moves_are_quiet_and_nothing_is_reconstructed():
    w = analyze_windows([ev(30, "S1")], path(0, [21000, 21002, 20999]), [], session_start=0,
                        session_end=100 * S, units_per_point=UPP, min_move_pt=20, health=[(0, True, ())])
    assert w[0].shadow == {} and w[0].classification == []


def test_data_quality_is_reported_not_hidden():
    evals = [ev(30, "S1", ok=False, blocks=("safety:market_data_not_ok",)), ev(60, "S2")]
    w = analyze_windows(evals, path(0, [21000 - i for i in range(30)]), [], session_start=0, session_end=300 * S,
                        units_per_point=UPP, min_move_pt=20,
                        health=[(0, True, ()), (40 * S, False, ("depth:requested",)), (100 * S, True, ())],
                        tape_breaks=[50 * S], alerts=[(60 * S, "depth_resync_budget_exhausted")])
    x = w[0]
    assert "DATA_QUALITY_ISSUES" in x.classification and x.md_ok_evaluations == 1
    assert x.not_ok_seconds == 60.0 and x.tape_breaks == 1 and x.alerts == ["depth_resync_budget_exhausted"]


# ---------------------------------------------------------------------------- end to end (real replay)

def _tool():
    spec = importlib.util.spec_from_file_location(
        "gap_report", Path(__file__).resolve().parents[2] / "tools" / "gap_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tool():
    return _tool()


def test_replayed_session_with_a_move_and_no_candidate(tool, tmp_path):
    d = tmp_path / "2026-09-22" / "s"
    write_hrec(d, trend(-1, minutes=12, t0=PRE_RTH, step_every=1).events,
               meta={"contract_spec": dict(SPEC.to_params())})
    out = "\n".join(tool.run(str(d), 20))
    assert "real signals     0" in out
    assert "MOVE_WITHOUT_CANDIDATE:SHORT" in out and "DATA_QUALITY_ISSUES" not in out
    assert "safety:outside_authorized_entry_hours" in out                 # why nothing was sent
    assert "SHORT excursion  MFE 35.75 pt" in out and RECONSTRUCTED_LABEL in out
    assert "no order was sent" in out and "P&L" not in out.replace("not P&L", "")


def test_replayed_session_with_real_signals_reports_each_window(tool, tmp_path):
    d = tmp_path / "2026-09-22" / "s"
    write_hrec(d, trend(+1, minutes=11).events, meta={"contract_spec": dict(SPEC.to_params())})
    lines = tool.run(str(d), 20)
    out = "\n".join(lines)
    assert "real signals     3" in out and sum(1 for x in lines if x.startswith("WINDOW")) == 4
    assert "MOVE_WITHOUT_CANDIDATE" not in out                              # nothing fabricated
    assert "opening signal   S" in out and "(shadow)" in out and "REARM" in out


def test_gap_analysis_writes_nothing(tool, tmp_path):
    d = tmp_path / "2026-09-22" / "s"
    write_hrec(d, trend(+1, minutes=11).events, meta={"contract_spec": dict(SPEC.to_params())})
    before = sorted(p.name for p in d.iterdir())
    tool.run(str(d), 20)
    assert sorted(p.name for p in d.iterdir()) == before
