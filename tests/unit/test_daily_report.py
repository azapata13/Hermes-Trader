import json

from tools.daily_report import best_per_setup, build, meets, wilson
from tools.day_analysis import analyse_day, stop_tests, synthetic_day
from tools.scalp_lab import build_ctx, simulate_day, summarize


def test_wilson_interval_known_value():
    lo, hi = wilson(12, 20)
    assert round(lo, 2) == 0.39 and round(hi, 2) == 0.78


def _reports(tmp_path):
    bars, ctx = synthetic_day()
    (tmp_path / "2026-11-02.json").write_text(json.dumps(
        [{"day": analyse_day(bars, ctx), "stop_tests": stop_tests(bars, 0.25)}], default=str))
    rows = summarize(simulate_day(build_ctx(bars, None), "2026-11-02", 0.75), 0.75)
    (tmp_path / "scalp-2026-11-02.json").write_text(json.dumps(
        {"days": ["2026-11-02"], "cost": 0.75, "min_trades": 5, "summary": rows}, default=str))
    return rows


def test_report_has_all_sections_and_flags_missing_files(tmp_path):
    from datetime import date
    _reports(tmp_path)
    text = build(date(2026, 11, 2), tmp_path)
    for h in ("## 1. En bref", "## 2. Le marché", "## 3. Les meilleurs tests", "## 4. Tous les tests",
              "## 5. Test du déplacement du stop", "## 6. Ce qu'on fait demain", "## 7. Les maths"):
        assert h in text
    assert "scalp-10-jours.json" in text  # missing files are reported, not invented
    assert "Seuil de rentabilité" in text and "Espérance par trade" in text


def test_decision_uses_a_valid_exit_when_one_exists(tmp_path):
    rows = _reports(tmp_path)
    best = best_per_setup(rows, 5)
    for name in {r["setup"] for r in rows}:
        if any(meets(r, 5) for r in rows if r["setup"] == name):
            assert meets(best[name], 5)
