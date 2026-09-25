"""Deterministic replay of Felipe's REAL MNQ recordings (no TWS needed, never runs in CI).

    HERMES_RECORDING=~/hermes-data/recordings/2026-09-24 python -m pytest -m live tests/live/test_real_recording.py -s

Replays the latest session under ``HERMES_RECORDING`` (default ``~/hermes-data/recordings``) twice
and requires identical raw digests, checkpoint sequences and final state hashes (raw-event
reproducibility). When the session carries live checkpoints written by the same code, the
live-vs-replay comparison must be EQUIVALENT. Pre-C6 recordings carry none: raw reproducibility only.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

ROOT = Path(os.path.expanduser(os.environ.get("HERMES_RECORDING", "~/hermes-data/recordings")))


@pytest.mark.skipif(not ROOT.exists(), reason=f"no recordings under {ROOT}")
def test_real_recording_replays_deterministically():
    from hermes.replay.runner import ReplayOptions, replay_session
    from tools.replay_report import find_session, render

    session = find_session(ROOT)
    assert session is not None, f"no session under {ROOT}"
    a = replay_session(session)
    b = replay_session(session, ReplayOptions(policy=a.policy))
    print("\n" + "\n".join(render(a)))
    assert a.raw_events > 0 and a.internal_errors == 0
    assert a.integrity.raw_digest == b.integrity.raw_digest
    assert [(c.key(), c.hash) for c in a.checkpoints] == [(c.key(), c.hash) for c in b.checkpoints]
    assert a.final_hash == b.final_hash
    if a.live_compare is not None:
        assert a.live_compare.equivalent, a.live_compare_status
