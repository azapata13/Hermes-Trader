"""V1 Pro D5/D7 analytics: shadow vs real separation, outcome statistics, similarity (>= 30), store."""

from __future__ import annotations

import json
import time

import pytest

from hermes.analytics.stats import (
    HISTORY_MIN_SAMPLES,
    approvals_by_proposal,
    comparable,
    disposition,
    excursion_quality,
    missed_report,
    outcome_stats,
)
from hermes.analytics.store import SetupStore, load_rows
from hermes.decision.features import COMPARABLE_FIELDS, FEATURES_VERSION

S = 10**9
FEAT = {"features_version": str(FEATURES_VERSION), "direction": "SHORT", "regime_5m": "SHORT",
        "setup_1m": "SHORT", "trigger_30s": "SHORT", "vwap": "with", "tape": "with", "ofi": "with",
        "sweep": "neutral", "session": "RTH", "time_bucket": "14:30Z", "risk_bucket": "<=10pt"}


def row(sid, *, status="COMPLETE", mfe=40, mae=8, risk=40, r1=True, r2=False, r3=False, stopped=False,
        close="EXPIRED", after=None, execution="SHADOW", features=None, proposal=None, created=0,
        mfe_t=60, stop_t=None, rearmed_by=None, horizons=(8, 20, 24, 30, 40)):
    return {
        "schema": 1, "setup_id": sid, "proposal_id": proposal or ("P" + sid[1:]), "direction": "SHORT",
        "units_per_point": 4, "point_value_usd": 2.0, "entry_units": 124155, "stop_units": 124195,
        "risk_units": risk, "created_wall_ns": created * S, "window_close_status": close,
        "phase": "ENDED", "rearmed_by": rearmed_by, "horizons_s": [60, 180, 300, 600, 900],
        "outcome": {"status": status, "mfe_units": mfe, "mae_units": mae,
                    "mfe_wall_ns": (created + mfe_t) * S, "stop_touched": stopped,
                    "stop_wall_ns": None if stop_t is None else (created + stop_t) * S,
                    "r_wall_ns": [1 if r1 else None, 1 if r2 else None, 1 if r3 else None],
                    "horizon_units": list(horizons), "mfe_after_close_units": after},
        "features": dict(features or FEAT), "execution": execution,
    }


# ---------------------------------------------------------------------------- statistics

def test_outcome_stats_definitions():
    rows = [row("S1", mfe=40, mae=4, r1=True, r2=True), row("S2", mfe=8, mae=40, r1=False, stopped=True,
            status="STOPPED"), row("S3", mfe=80, mae=12, r1=True, r2=True, r3=False),
            row("S4", status="INCOMPLETE", mfe=400)]                       # excluded
    s = outcome_stats(rows)
    assert s.samples == 3 and (s.plus_1r, s.plus_2r, s.plus_3r, s.stop_first) == (2, 2, 0, 1)
    assert s.median_mfe_pt == 10.0 and s.median_mae_pt == 3.0                 # 40/4, 12/4 units -> pt
    assert s.median_result_5m_pt == 6.0 and s.median_result_10m_pt == 7.5


def test_shadow_and_real_are_never_mixed():
    rows = [row(f"S{i}") for i in range(5)] + [row("R1", execution="REAL", mfe=4, r1=False)]
    assert outcome_stats(rows, "SHADOW").samples == 5
    assert outcome_stats(rows, "REAL").samples == 1
    rep = missed_report([r for r in rows if r["execution"] == "SHADOW"])
    assert rep.real.samples == 0 and rep.lines()[0].startswith("REAL TRADES      none executed")
    assert "SHADOW SETUPS" in rep.lines()[1]


@pytest.mark.parametrize("n,shown", [(HISTORY_MIN_SAMPLES - 1, False), (HISTORY_MIN_SAMPLES, True)])
def test_history_needs_30_comparable_setups(n, shown):
    rows = [row(f"S{i}") for i in range(n)]
    other = dict(FEAT, trigger_30s="NEUTRAL")
    rows += [row(f"X{i}", features=other) for i in range(50)]                 # not comparable
    c = comparable(FEAT, rows)
    assert c.stats.samples == n and c.displayable is shown
    assert "similarity v1" in c.selection_definition and all(f in c.selection_definition for f in COMPARABLE_FIELDS)
    assert f"{n} recorded shadow setups" in c.source


def test_comparable_excludes_itself_incomplete_and_unversioned_rows():
    rows = [row(f"S{i}") for i in range(30)] + [row("Sself")] + [row("Sinc", status="INCOMPLETE")]
    rows.append(row("Sold", features={k: v for k, v in FEAT.items() if k != "features_version"}))
    c = comparable(FEAT, rows, exclude_setup_id="Sself")
    assert c.stats.samples == 30
    assert comparable({"direction": "SHORT"}, rows).key is None              # missing fields -> unavailable


# ---------------------------------------------------------------------------- dispositions / missed

def test_dispositions_join_the_approval_journal():
    approvals = approvals_by_proposal([
        {"proposal_id": "PA", "action": "ENTER", "accepted_human_intent": True},
        {"proposal_id": "PB", "action": "REJECT", "accepted_human_intent": True},
        {"proposal_id": "PC", "action": "ENTER", "accepted_human_intent": False},     # failed closed
    ])
    assert disposition(row("SA", proposal="PA"), approvals) == "APPROVED_INTENT"
    assert disposition(row("SB", proposal="PB"), approvals) == "REJECTED"
    assert disposition(row("SC", proposal="PC", close="EXPIRED"), approvals) == "EXPIRED_UNAPPROVED"
    assert disposition(row("SD", close="STALE"), approvals) == "EXPIRED_UNAPPROVED"
    assert disposition(row("SE", close="INVALIDATED"), approvals) == "INVALIDATED"


def test_missed_opportunity_report_is_objective():
    rows = [
        row("S1", close="STALE", after=85, mfe=90, mae=4, r1=True, r2=True),        # +21.25 pt after STALE, +2R
        row("S2", close="EXPIRED", after=10, mfe=20, mae=40, r1=False, stopped=True, status="STOPPED", stop_t=30),
        row("S3", close="EXPIRED", after=None, status="INCOMPLETE"),
        row("S4", close="EXPIRED", after=45, mfe=50, mae=6, r1=True, rearmed_by="S5"),
    ]
    rep = missed_report(rows, [])
    assert rep.detected == 4 and rep.eligible == 3 and rep.incomplete == 1
    assert rep.by_disposition == {"EXPIRED_UNAPPROVED": 4}
    assert rep.continued_after_close_1r == 2 and rep.rearmed == 1
    assert rep.stale_then_20pt == ["S1"] and rep.stale_then_2r == ["S1"]
    assert rep.quick_invalidation == ["S2"] and rep.low_mae_high_mfe == ["S1"]
    text = "\n".join(rep.lines())
    assert "profit" not in text.lower() and "p&l" not in text.lower()


def test_excursion_quality_is_not_profit_and_ignores_incomplete():
    assert excursion_quality(row("S1", mfe=40, mae=9)) == 4.0
    assert excursion_quality(row("S2", status="INCOMPLETE")) is None


# ---------------------------------------------------------------------------- store

def test_store_load_is_tolerant_and_counts_what_it_skips(tmp_path):
    p = tmp_path / "setups.jsonl"
    good = row("S1")
    lines = [json.dumps(good), "not json", json.dumps({"schema": 1}), json.dumps(dict(row("S2"), schema=99)),
             json.dumps(good), json.dumps(row("S3", status="OPEN")), json.dumps(row("S4"))]
    p.write_text("\n".join(lines) + "\n")
    r = load_rows(p)
    assert [x["setup_id"] for x in r.rows] == ["S1", "S4"]
    assert (r.malformed, r.unsupported_schema, r.duplicates, r.unfinished) == (2, 1, 1, 1)


def test_store_appends_asynchronously_and_is_idempotent(tmp_path):
    p = tmp_path / "sub" / "setups.jsonl"
    st = SetupStore(p)
    st.start()
    assert st.add(row("S1")) and not st.add(row("S1")) and not st.add(row("S2", status="OPEN"))
    st.close()
    deadline = time.monotonic() + 2
    while not p.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [x["setup_id"] for x in load_rows(p).rows] == ["S1"]
    assert [x["setup_id"] for x in SetupStore(p).rows] == ["S1"]          # reloaded on the next start


def test_setup_report_tool_prints_definitions_and_separates_real(tmp_path, capsys):
    import importlib.util
    from pathlib import Path
    p = tmp_path / "setups.jsonl"
    p.write_text("\n".join(json.dumps(row(f"S{i}", close="STALE", after=90, mfe=90, mae=4, r2=True))
                           for i in range(3)) + "\n")
    spec = importlib.util.spec_from_file_location(
        "setup_report", Path(__file__).resolve().parents[2] / "tools" / "setup_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main([str(p), "--approvals", str(tmp_path / "none.jsonl")]) == 0
    out = capsys.readouterr().out
    assert "never P&L" in out and "REAL TRADES      none executed" in out
    assert "STALE/EXPIRED then +20 pt   3" in out and "continuation = +1R" in out


def test_older_rows_without_optional_fields_stay_usable():
    old = row("S1")
    del old["horizons_s"]
    old.pop("features")
    s = outcome_stats([old, row("S2")])
    assert s.samples == 2 and s.median_result_5m_pt == 6.0      # default horizons assumed for v1 rows
    assert comparable(FEAT, [old]).stats.samples == 0             # no characteristics -> never comparable
