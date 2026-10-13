"""V1 Pro outcome of every ACTIONABLE setup: SHADOW (not taken / no execution) vs REAL (executed).

SHADOW is a post-setup measurement and is never P&L; REAL comes only from executed trades (none in V1).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from hermes.analytics.outcome import (
    OUTCOME_RULE,
    SHADOW_LABEL,
    RealTrade,
    RealTradeStore,
    disposition_text,
    performance_report,
    real_trade_from_row,
    shadow_result,
    shadow_result_lines,
)
from tests.unit.test_analytics import row

S = 10**9


# ---------------------------------------------------------------------------- shadow result

def test_shadow_result_targets_mfe_mae_and_outcome_rule():
    # SHORT, entry 31038.75, risk 10 pt; MFE 24 pt, MAE 4 pt, 15 min result +18 pt
    r = shadow_result(row("S1", mfe=96, mae=16, horizons=(8, 20, 40, 60, 72)))
    assert (r.mfe_pt, r.mae_pt, r.risk_pt) == (24.0, 4.0, 10.0)
    assert dict(r.hits) == {10: True, 20: True, 30: False}
    assert r.outcome_pt == 18.0 and r.duration_s == 900 and r.final and r.execution == "SHADOW"


def test_stop_first_outcome_is_minus_risk_and_targets_count_only_before_the_stop():
    r = shadow_result(row("S2", status="STOPPED", stopped=True, stop_t=95, mfe=44, mae=40, risk=40))
    assert r.outcome_pt == -10.0 and r.stop_touched and r.duration_s == 95
    assert dict(r.hits) == {10: True, 20: False, 30: False}      # MFE frozen at the stop touch: 11 pt


def test_open_or_incomplete_is_never_given_an_outcome():
    for status in ("OPEN", "INCOMPLETE"):
        r = shadow_result(row("S3", status=status, mfe=400))
        assert r.outcome_pt is None and not r.final and r.duration_s is None


def test_shadow_result_refuses_real_rows():
    with pytest.raises(ValueError):
        shadow_result(row("R1", execution="REAL"))


def test_shadow_lines_are_labelled_and_never_say_profit():
    lines = shadow_result_lines(shadow_result(row("S1", mfe=96, mae=16, horizons=(8, 20, 40, 60, 72))),
                                disposition_text(None, "EXPIRED"))
    text = "\n".join(lines)
    assert lines[0].startswith("⚪ SETUP NOT TAKEN") and "SHADOW RESULT" in text and lines[-1] == SHADOW_LABEL
    assert "MFE +24 pts" in text and "MAE -4 pts" in text and "+10: YES · +20: YES · +30: NO" in text
    assert "Outcome: +18 pts" in text
    low = text.lower().replace("not real p&l", "")
    assert "p&l" not in low and "profit" not in low and "gain" not in low


@pytest.mark.parametrize("human,close,start", [
    ("REJECT recorded", "EXPIRED", "⚪ SETUP NOT TAKEN — rejected"),
    (None, "EXPIRED", "⚪ SETUP NOT TAKEN — window expired"),
    (None, "INVALIDATED", "⚪ SETUP NOT TAKEN — invalidated"),
    ("ENTER intent recorded (no order sent)", "EXPIRED", "🟡 ENTER INTENT RECORDED — no order sent"),
])
def test_disposition_headlines(human, close, start):
    assert disposition_text(human, close).startswith(start)


# ---------------------------------------------------------------------------- real trades

def _real(**kw):
    base = dict(trade_id="T1", setup_id="S1", direction="SHORT", symbol="MNQZ6", entry_reference=30680.0,
                actual_entry=30679.75, stop=30690.0, actual_exit=30661.75, quantity=1, point_value_usd=2.0,
                entry_wall_ns=100 * S, exit_wall_ns=400 * S, exit_reason="target", mfe_pt=24.0, mae_pt=3.0)
    base.update(kw)
    return RealTrade(**base)


def test_real_trade_points_pnl_duration_and_label():
    t = _real()
    assert t.points == 18.0 and t.pnl_usd == 36.0 and t.duration_s == 300 and t.execution == "REAL"
    assert t.row()["label"] == "REAL TRADE"


def test_real_trade_validation_and_row_roundtrip():
    with pytest.raises(ValueError):
        _real(execution="SHADOW")
    with pytest.raises(ValueError):
        _real(direction="FLAT")
    with pytest.raises(ValueError):
        _real(exit_wall_ns=10 * S)
    with pytest.raises(ValueError):
        real_trade_from_row({"execution": "SHADOW"})
    assert real_trade_from_row(_real().row()) == _real()


def test_real_store_is_separate_and_skips_bad_rows(tmp_path):
    store = RealTradeStore(tmp_path / "real.jsonl")
    store.add(_real())
    with pytest.raises(TypeError):
        store.add(row("S1"))                                     # a shadow row can never be recorded as REAL
    with (tmp_path / "real.jsonl").open("a") as f:
        f.write(json.dumps({"execution": "REAL", "trade_id": "broken"}) + "\n" + "not json\n")
    trades, bad = store.load()
    assert [t.trade_id for t in trades] == ["T1"] and bad == 2


# ---------------------------------------------------------------------------- performance report

def _approval(pid, action, accepted=True):
    # same shape as ApprovalAuditEntry.to_dict() (human_approvals.jsonl)
    return {"proposal_id": pid, "action": action, "accepted_human_intent": accepted,
            "human_response": {"action": action}, "authorizes_execution": False}


def test_performance_report_separates_decision_time_shadow_and_real():
    rows = [
        row("S1", mfe=96, mae=16, horizons=(8, 20, 40, 60, 72)),                       # expired, +18, +20 hit
        row("S2", status="STOPPED", stopped=True, stop_t=60, mfe=12, mae=40),         # rejected, -10
        row("S3", mfe=140, mae=4, horizons=(8, 20, 40, 60, 128)),                      # ENTER intent, +32, +30 hit
        row("S4", status="INCOMPLETE", mfe=400),                                       # excluded
        row("R9", execution="REAL"),                                                   # never counted as shadow
    ]
    approvals = [_approval("P2", "REJECT"), _approval("P3", "ENTER")]
    rep = performance_report(rows, approvals, [_real(), _real(trade_id="T2", actual_exit=30690.0)])
    assert rep.actionable == 4 and rep.taken_intent == 1 and rep.not_taken == 3
    assert rep.by_disposition == {"EXPIRED_UNAPPROVED": 2, "REJECTED": 1, "APPROVED_INTENT": 1}
    assert (rep.shadow_final, rep.shadow_excluded, rep.shadow_wins, rep.shadow_losses) == (3, 1, 2, 1)
    assert rep.target_hits == {10: 2, 20: 2, 30: 1}
    assert rep.missed_profitable == ["S1"] and rep.missed_20pt == ["S1"]                # S3 was an ENTER intent
    assert rep.shadow_avg_points == round((18 - 10 + 32) / 3, 2)
    assert (rep.real_trades, rep.real_wins, rep.real_losses) == (2, 1, 1)
    text = "\n".join(rep.lines())
    assert "DECISION-TIME" in text and "POST-SETUP SHADOW OUTCOME" in text and SHADOW_LABEL in text
    assert OUTCOME_RULE in text and "REAL TRADES (REAL TRADE)" in text


def test_performance_report_without_real_trades_says_none_executed():
    text = "\n".join(performance_report([row("S1")]).lines())
    assert "none executed" in text


def test_setup_report_tool_prints_the_performance_section(tmp_path, capsys):
    from hermes.analytics.store import SetupStore

    store = tmp_path / "setups.jsonl"
    s = SetupStore(store)
    s.start()
    s.add(row("S1", mfe=96, mae=16, horizons=(8, 20, 40, 60, 72)))
    s.close()
    spec = importlib.util.spec_from_file_location(
        "setup_report", Path(__file__).resolve().parents[2] / "tools" / "setup_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main([str(store), "--approvals", str(tmp_path / "none.jsonl"), "--real", str(tmp_path / "r.jsonl")]) == 0
    out = capsys.readouterr().out
    assert "PERFORMANCE" in out and "missed +20 pt 1" in out and "none executed" in out
