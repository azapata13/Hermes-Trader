"""Live smoke test against a real, running TWS (READ-ONLY). Never runs in CI.

Run explicitly on the Mac:
    HERMES_LIVE=1 python -m pytest -m live tests/live -s
Equivalent to: python -m hermes.app.run_live --duration 60
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live


@pytest.mark.skipif(os.environ.get("HERMES_LIVE") != "1", reason="set HERMES_LIVE=1 to run against TWS")
def test_live_60s_smoke():
    from hermes.app.run_live import LiveRuntime
    from hermes.config import load_config

    rt = LiveRuntime(load_config())
    summary = rt.run(60.0, install_signals=False)
    print(summary)
    assert summary["healthy"], summary["problems"]
    assert summary["replay_complete"] is True
