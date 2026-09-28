from tools import orderflow_report
from tests.support import BASE, DEPTH, TICK, TRADES, RawScript, write_hrec


def test_orderflow_report_finds_latest_valid_pre_shutdown_c8_state(tmp_path, capsys):
    sc = RawScript()
    sc.bootstrap()
    sc.seed_book(rows=10)
    sc.advance(600)
    sc.tick()

    sc.advance(10)
    sc.trade(TRADES, BASE + TICK, 4)
    sc.advance(50)
    sc.depth(DEPTH, 0, 1, 0, BASE + TICK, 14)
    sc.advance(600)
    sc.tick()

    sc.request("cancelMktDepth", DEPTH)
    sc.closed()

    d = write_hrec(tmp_path / "session", sc.events)
    assert orderflow_report.main([str(d)]) == 0
    out = capsys.readouterr().out

    assert "structure" in out
    assert "patterns" in out
    assert "absorption-compatible context" in out
    assert "compatible_cap is an aggregate upper bound" in out
    assert "do not infer a directional trading signal" in out
