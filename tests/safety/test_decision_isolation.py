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


def test_human_response_module_has_no_order_path():
    """C9e: the ENTER/REJECT response is pure data + pure checks. It imports only decision-layer and
    stdlib-data modules, defines no I/O, and its only 'authorization' property is hard-wired False."""
    path = DECISION / "response.py"
    mods = {m for _, m in _imports(path)}
    assert mods <= {"__future__", "dataclasses", "enum", "hermes.decision.approval", "hermes.decision.lifecycle",
                    "hermes.decision.reasons", "hermes.decision.safety"}, mods
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not calls & {"open", "exec", "eval", "compile", "__import__", "print", "input"}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attrs & {"send", "post", "request", "urlopen", "connect", "submit", "execute", "write"}, attrs

    from hermes.decision.response import HumanApprovalResponse, execution_prerequisites
    public = {n for n in dir(HumanApprovalResponse) if not n.startswith("_")}
    assert public == {"action", "setup_id", "proposal_id", "approval_view_id", "response_wall_ns", "response_seq",
                      "note", "schema_version", "authorizes_execution"}, public
    src = path.read_text(encoding="utf-8")
    assert 'return False' in src.split("def authorizes_execution", 1)[1].split("def ", 1)[0]
    assert "ExecutionPrerequisites(False," in src                         # never satisfiable in Phase C9
    assert execution_prerequisites.__module__ == "hermes.decision.response"


def test_decision_persistence_and_runtime_have_no_broker_path():
    """C9f: the decision journal persistence (hermes/replay/decisions.py) and the runtime consumer
    import no broker / app / network code; the live wiring passes only the read-only engine."""
    mods = {m for _, m in _imports(ROOT / "hermes" / "replay" / "decisions.py")}
    assert mods <= {"__future__", "dataclasses", "hashlib", "json", "pathlib", "typing",
                    "hermes.decision.runtime", "hermes.replay.fingerprint"}, mods
    rt_mods = {m for _, m in _imports(DECISION / "runtime.py")}
    assert not any(m.startswith(FORBIDDEN_PREFIXES) for m in rt_mods), rt_mods
    for f in (ROOT / "hermes" / "replay" / "decisions.py", DECISION / "runtime.py", ROOT / "hermes" / "market" / "rolling.py"):
        src = f.read_text(encoding="utf-8")
        for word in ("placeOrder", "cancelOrder", "reqGlobalCancel", "EClient", "RequestGateway", "gateway"):
            assert word not in src, f"{f.name} mentions {word}"
