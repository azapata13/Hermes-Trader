#!/usr/bin/env python3
from pathlib import Path
import sys

repo = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / "hermes-trading").expanduser().resolve()

def load(rel):
    p = repo / rel
    if not p.exists():
        raise SystemExit(f"missing: {p}")
    return p, p.read_text()

def repl(s, old, new, label):
    n = s.count(old)
    if n != 1:
        raise SystemExit(f"{label}: expected exactly 1 match, found {n}")
    return s.replace(old, new, 1)

p, s = load("hermes/replay/fingerprint.py")
s = repl(s, "HASH_VERSION = 1\n", "HASH_VERSION = 2\n", "bump HASH_VERSION")
s = repl(
    s,
    '"hermes/market/orderbook.py", "hermes/market/health.py", "hermes/market/classify.py", "hermes/market/tape.py",\n',
    '"hermes/market/orderbook.py", "hermes/market/health.py", "hermes/market/classify.py", "hermes/market/tape.py",\n'
    '    "hermes/market/metrics.py",\n',
    "include metrics.py in code fingerprint",
)
s = repl(
    s,
    '''        bars_part,
        inst.sessions.snapshot() if inst.sessions is not None else None,
    )
''',
    '''        bars_part,
        inst.sessions.snapshot() if inst.sessions is not None else None,
        inst.metrics.fingerprint_state(),
    )
''',
    "include metrics state in engine fingerprint",
)
old_doc = '''Excluded by construction: object addresses, process clocks, receive timestamps of individual
events (the recorded ``seq`` already fixes ordering), anything iteration-order dependent. Every
completed bar appears in the checkpoint stream (one checkpoint per bar close), so the full bar
history is covered without hashing it wholesale each time.
'''
new_doc = '''Excluded by construction: object addresses, process clocks and anything iteration-order
dependent. C7 rolling metrics intentionally include their bounded recorded ``recv_mono_ns``
timestamps because deterministic window eviction depends on them. Every completed bar appears
in the checkpoint stream (one checkpoint per bar close), so the full bar history is covered
without hashing it wholesale each time.
'''
if old_doc in s:
    s = s.replace(old_doc, new_doc, 1)
p.write_text(s)

p, s = load("hermes/app/run_live.py")
if 'HERMES_VERSION = "0.7.0-c7"' not in s:
    s = repl(s, 'HERMES_VERSION = "0.6.0-c6"\n', 'HERMES_VERSION = "0.7.0-c7"\n', "bump live version")
p.write_text(s)

test_path = repo / "tests/unit/test_metrics_fingerprint.py"
test_path.write_text('''from hermes.replay import fingerprint as fp
from tests.support import Harness, RawScript


def test_metrics_bounded_state_is_covered_by_fingerprint():
    sc = RawScript()
    sc.bootstrap()
    sc.seed_book(rows=10)
    sc.advance(600)
    sc.tick()

    h = Harness().run(sc)
    inst = h.engine.instruments[1]

    before = fp.state_hash(h.engine)
    inst.metrics.advance(h.engine.last_mono_ns + 1)
    after = fp.state_hash(h.engine)

    assert after != before
''', encoding="utf-8")

print("C7b2 fingerprint/replay integration applied successfully")
