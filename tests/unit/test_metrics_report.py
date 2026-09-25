from tools import metrics_report
from tests.support import BASE, DEPTH, TICK, TRADES, RawScript, write_hrec


def test_metrics_report_finds_latest_valid_pre_shutdown_state(tmp_path, capsys):
    sc = RawScript()
    sc.bootstrap()
    sc.seed_book(rows=10)
    sc.advance(600)
    sc.tick()
    sc.advance(100)
    sc.depth(DEPTH, 0, 1, 1, BASE, 20)
    sc.trade(TRADES, BASE + TICK, 3)
    sc.request("cancelMktDepth", DEPTH)
    sc.closed()

    d = write_hrec(tmp_path / "session", sc.events)
    assert metrics_report.main([str(d)]) == 0
    out = capsys.readouterr().out
    assert "observation" in out
    assert "imbalance" in out
    assert "trade flow" in out
    assert "velocity" in out
    assert "metrics are measurements only" in out
