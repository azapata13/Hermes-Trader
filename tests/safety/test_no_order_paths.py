"""Static guarantees that Phase C contains no order execution path (decision 11)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from hermes.config import load_config
from hermes.ibkr.readonly import ALLOWED_METHODS, FORBIDDEN_METHODS

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "hermes"

FORBIDDEN_IMPORT_PREFIXES = ("ibapi.order", "ibapi.order_cancel", "ibapi.order_condition", "ibapi.order_state")

# The only module allowed to subclass / construct / import EClient.
READONLY_MODULE = "hermes/ibkr/readonly.py"
# The only Hermès modules allowed to send requests / construct the client.
GATEWAY_MODULE = "hermes/ibkr/gateway.py"
CLIENT_FACTORY_MODULES = frozenset({READONLY_MODULE, "hermes/ibkr/session.py"})
# EClient request method names (TWS calls). Inside the hermes package only the gateway calls them.
REQUEST_METHODS = frozenset(n for n in ALLOWED_METHODS
                            if n.startswith(("req", "cancel")) and not n.endswith("ProtoBuf"))


def scan_source(src: str, rel: str) -> list[str]:
    """Return a list of violations found in one module's source."""
    problems: list[str] = []
    tree = ast.parse(src, filename=rel)
    in_pkg = rel.startswith("hermes/")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_METHODS:
            problems.append(f"{rel}:{node.lineno} uses forbidden attribute .{node.attr}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_METHODS:
            problems.append(f"{rel}:{node.lineno} uses forbidden name {node.id}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(FORBIDDEN_IMPORT_PREFIXES):
                problems.append(f"{rel}:{node.lineno} imports {node.module}")
            if node.module == "ibapi":
                for alias in node.names:
                    if alias.name.startswith("order"):
                        problems.append(f"{rel}:{node.lineno} imports ibapi.{alias.name}")
            if rel != READONLY_MODULE and any(a.name == "EClient" for a in node.names):
                problems.append(f"{rel}:{node.lineno} imports EClient (use ReadOnlyClient)")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(FORBIDDEN_IMPORT_PREFIXES):
                    problems.append(f"{rel}:{node.lineno} imports {alias.name}")
        if rel != READONLY_MODULE:
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                    if name in ("EClient", "ReadOnlyClient"):
                        problems.append(f"{rel}:{node.lineno} subclasses {name}")
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                if name == "EClient":
                    problems.append(f"{rel}:{node.lineno} constructs EClient (use ReadOnlyClient)")
                if in_pkg and name == "ReadOnlyClient" and rel not in CLIENT_FACTORY_MODULES:
                    problems.append(f"{rel}:{node.lineno} constructs ReadOnlyClient outside the supervisor")
                if (in_pkg and rel != GATEWAY_MODULE and isinstance(fn, ast.Attribute)
                        and fn.attr in REQUEST_METHODS):
                    problems.append(f"{rel}:{node.lineno} calls TWS request .{fn.attr} outside RequestGateway")
    return problems


def _modules() -> list[Path]:
    """Everything shipped: the hermes package AND tools/ (diagnostics included)."""
    files = [p for root in (PKG, ROOT / "tools") for p in root.rglob("*.py")]
    return sorted(p for p in files if "__pycache__" not in p.parts)


def test_package_has_no_order_paths():
    problems: list[str] = []
    for path in _modules():
        rel = path.relative_to(ROOT).as_posix()
        problems += scan_source(path.read_text(encoding="utf-8"), rel)
    assert problems == [], "\n".join(problems)


def test_scanner_detects_violations():
    bad = (
        "from ibapi.order import Order\n"
        "import ibapi.order_cancel\n"
        "from ibapi.client import EClient\n"
        "class X(EClient):\n"
        "    pass\n"
        "c = EClient(None)\n"
        "c.placeOrder(1, None, None)\n"
        "getattr(c, 'x').cancelOrder(1)\n"
        "c.exerciseOptions()\n"
        "r = ReadOnlyClient(w)\n"
        "r.reqMktDepth(1, None, 10, False, [])\n"
    )
    problems = scan_source(bad, "hermes/fake.py")
    joined = "\n".join(problems)
    for needle in ("imports ibapi.order", "imports ibapi.order_cancel", "imports EClient", "subclasses EClient",
                   "constructs EClient", ".placeOrder", ".cancelOrder", ".exerciseOptions",
                   "constructs ReadOnlyClient outside", "reqMktDepth outside RequestGateway"):
        assert needle in joined, needle
    # diagnostics under tools/ may use a ReadOnlyClient directly, never EClient or order methods
    tool = "r = ReadOnlyClient(w)\nr.reqMktDepth(1, None, 10, False, [])\n"
    assert scan_source(tool, "tools/phase_b/x.py") == []


def test_phase_b_diagnostics_moved_and_ported():
    for name in ("connection_test.py", "mnq_live_test.py", "orderflow_test.py"):
        assert not (PKG / "ibkr" / name).exists()
        src = (ROOT / "tools" / "phase_b" / name).read_text(encoding="utf-8")
        assert "ReadOnlyClient(app)" in src


def test_no_execution_package():
    assert not (PKG / "execution").exists(), "hermes/execution must not exist in Phase C"


def test_default_config_is_read_only():
    cfg = load_config()
    assert cfg.safety.orders_enabled is False
    assert cfg.ibkr.read_only is True


def test_core_modules_do_not_import_ibapi():
    """Raw events, market model and config must be usable without ibapi (replay without TWS)."""
    code = (
        "import sys\n"
        "import hermes.config, hermes.core.clock, hermes.ibkr.raw_events, hermes.ibkr.codes,\\\n"
        "       hermes.ibkr.market_rules, hermes.market.events, hermes.market.pricegrid,\\\n"
        "       hermes.market.orderbook, hermes.ibkr.normalizer, hermes.ibkr.contracts, hermes.ibkr.errors,\\\n"
        "       hermes.market.engine, hermes.market.health, hermes.market.snapshot,\\\n"
        "       hermes.storage.codec, hermes.storage.recorder, hermes.storage.reader, hermes.core.latency\n"
        "bad = [m for m in sys.modules if m == 'ibapi' or m.startswith('ibapi.')]\n"
        "assert not bad, bad\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=ROOT)
