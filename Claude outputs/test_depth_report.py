"""tools/depth_report.py: per-generation depth forensics from a raw recording (replay only)."""

from __future__ import annotations

from tests.support import BASE, BBO, DEPTH, TICK, write_hrec
from tests.unit.test_replay import ready, ticks


def test_depth_report_traces_each_resync(tmp_path, capsys):
    from tools import depth_report
    sc = ready()
    sc.depth(DEPTH, 9, 1, 1, BASE, 1)                  # update at a non-existent row -> structural violation
    ticks(sc, 300)
    sc.request("cancelMktDepth", DEPTH)
    sc.request("reqMktDepth", 20_001, num_rows=10)
    sc.seed_book(depth=20_001, l1=0)
    sc.depth(DEPTH, 0, 1, 1, BASE, 5)                  # late callback of the replaced generation
    sc.bbo(BBO, BASE - TICK, BASE)                     # BBO moves, depth top does not
    ticks(sc, 4000)
    d = write_hrec(tmp_path / "s", sc.events)
    assert depth_report.main([str(d)]) == 0
    out = capsys.readouterr().out
    assert "initial subscription: reqId=10001" in out and "resync #1: reqId=20001" in out
    assert "structural_violation" in out and "UPDATE BID pos=9" in out and "position_out_of_range" in out
    assert "trigger: structural_violation" in out and "persistent_bbo_mismatch" in out
    assert "old-generation callbacks for this reqId after replacement: 1" in out
