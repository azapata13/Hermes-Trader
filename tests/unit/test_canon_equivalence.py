"""C9f checkpoint hashing: the faster canon() is byte-identical to the original canonicalization."""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json

import pytest

from hermes.replay import fingerprint as fp
from tests.support import Harness
from tests.unit.test_candidate import trend


def _bytes(f, obj) -> bytes:
    return json.dumps(f(obj), separators=(",", ":"), ensure_ascii=True).encode()


def reference_state_hash(engine) -> str:
    return hashlib.sha256(_bytes(fp.canon_reference, fp.state_summary(engine))).hexdigest()


class IE(enum.IntEnum):
    A = 3


class SE(str, enum.Enum):
    B = "b"


class FE(enum.Enum):
    C = 1.5


@dataclasses.dataclass(frozen=True)
class DC:
    a: int
    b: tuple
    c: object = None


class Tup(tuple):
    pass


ODD = [None, True, False, 0, -7, 2**70, "x", "é", 1.25, -0.0, float("inf"), IE.A, SE.B, FE.C,
       (1, (2, (3, "a"))), [1, [2.5]], {"b": 1, "a": (2, 3)}, {3: "x", 1: None}, {1, 2, 3}, frozenset({"a"}),
       DC(1, (IE.A, SE.B), {"k": {1.0}}), Tup((1, 2)), (DC(2, ()), [FE.C, {"z": [True]}])]


@pytest.mark.parametrize("obj", ODD, ids=range(len(ODD)))
def test_canon_identical_on_odd_objects(obj):
    assert _bytes(fp.canon, obj) == _bytes(fp.canon_reference, obj)


def test_canon_rejects_the_same_objects():
    for bad in (object(), b"bytes", 1j):
        with pytest.raises(TypeError):
            fp.canon_reference(bad)
        with pytest.raises(TypeError):
            fp.canon(bad)


def test_state_hash_identical_at_every_event_of_a_scenario():
    h = Harness()
    for k, raw in enumerate(trend(+1, minutes=7).events):
        h.feed([raw])
        if k % 7 == 0:
            assert fp.state_hash(h.engine) == reference_state_hash(h.engine), raw.seq


def test_state_hash_identical_on_a_busy_market():
    import tempfile
    from pathlib import Path

    from hermes.storage.reader import iter_raw_events
    from tools.c9_benchmark import synthetic_session
    d = synthetic_session(0.75, Path(tempfile.mkdtemp()) / "s", seed=5)
    h = Harness()
    for k, raw in enumerate(iter_raw_events(d)):
        h.feed([raw])
        if k % 997 == 0:
            assert fp.state_hash(h.engine) == reference_state_hash(h.engine)
    assert fp.state_hash(h.engine) == reference_state_hash(h.engine)
