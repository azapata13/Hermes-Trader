"""D1 Slack adapter isolation: transport may talk to Slack, never to IBKR or the order path."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SLACK = ROOT / "hermes" / "slack"


def _imports(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_slack_package_has_no_broker_or_order_path():
    files = sorted(SLACK.glob("*.py"))
    assert files
    forbidden_imports = ("ibapi", "hermes.ibkr")
    forbidden_words = ("placeOrder", "cancelOrder", "reqGlobalCancel", "EClient", "RequestGateway")
    problems = []
    for path in files:
        for mod in _imports(path):
            if mod.startswith(forbidden_imports):
                problems.append(f"{path.name} imports {mod}")
        src = path.read_text(encoding="utf-8")
        for word in forbidden_words:
            if word in src:
                problems.append(f"{path.name} mentions {word}")
    assert not problems, problems


def test_decision_package_still_does_not_import_slack():
    decision = ROOT / "hermes" / "decision"
    for path in decision.glob("*.py"):
        assert not any(mod.startswith(("slack", "hermes.slack")) for mod in _imports(path)), path.name
