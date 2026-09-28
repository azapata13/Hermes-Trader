"""C9 decision layer isolation: pure evidence/proposal code with no broker, network, clock or RNG path."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DECISION = ROOT / "hermes" / "decision"

ALLOWED_PREFIXES = ("__future__", "dataclasses", "typing", "enum", "collections", "math", "itertools",
                    "hermes.config", "hermes.market", "hermes.replay.fingerprint", "hermes.decision")
FORBIDDEN_PREFIXES = ("ibapi", "hermes.ibkr", "hermes.app", "hermes.storage", "socket", "time", "random",
                      "datetime", "threading", "asyncio", "subprocess", "urllib", "http", "requests",
                      "openai", "anthropic", "slack")


def _imports(path: Path) -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append((node.lineno, node.module))
    return out


def test_decision_package_exists_and_is_isolated():
    files = sorted(DECISION.glob("*.py"))
    assert files, "hermes/decision is missing"
    problems = []
    for f in files:
        for line, mod in _imports(f):
            if mod.startswith(FORBIDDEN_PREFIXES) or not mod.startswith(ALLOWED_PREFIXES):
                problems.append(f"{f.name}:{line} imports {mod}")
    assert not problems, problems


def test_decision_package_has_no_order_vocabulary():
    banned = ("placeOrder", "cancelOrder", "reqGlobalCancel", "ReadOnlyClient", "EClient", "RequestGateway")
    for f in DECISION.glob("*.py"):
        src = f.read_text(encoding="utf-8")
        for word in banned:
            assert word not in src, f"{f.name} mentions {word}"
