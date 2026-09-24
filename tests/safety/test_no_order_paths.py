"""Static guarantees that Phase C contains no order execution path (decision 11)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from hermes.config import load_config
from hermes.ibkr.readonly import FORBIDDEN_METHODS

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "hermes"

FORBIDDEN_IMPORT_PREFIXES = ("ibapi.order", "ibapi.order_cancel", "ibapi.order_condition", "ibapi.order_state")

# The only module allowed to subclass / construct EClient.
READONLY_MODULE = "hermes/ibkr/readonly.py"

# Phase B diagnostics predating ReadOnlyClient. TEMPORARY exception to the EClient rule only:
# they are still scanned for forbidden method names and imports. To be ported or moved in C3.
LEGACY_ECLIENT_EXCEPTIONS = frozenset({
    "hermes/ibkr/connection_test.py",
    "hermes/ibkr/mnq_live_test.py",
    "hermes/ibkr/orderflow_test.py",
})


def scan_source(src: str, rel: str) -> list[str]:
    """Return a list of violations found in one module's source."""
    problems: list[str] = []
    tree = ast.parse(src, filename=rel)
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
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(FORBIDDEN_IMPORT_PREFIXES):
                    problems.append(f"{rel}:{node.lineno} imports {alias.name}")
        if rel != READONLY_MODULE and rel not in LEGACY_ECLIENT_EXCEPTIONS:
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                    if name == "EClient":
                        problems.append(f"{rel}:{node.lineno} subclasses EClient (use ReadOnlyClient)")
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                if name == "EClient":
                    problems.append(f"{rel}:{node.lineno} constructs EClient (use ReadOnlyClient)")
    return problems


def _modules() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)


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
    )
    problems = scan_source(bad, "hermes/fake.py")
    joined = "\n".join(problems)
    for needle in ("imports ibapi.order", "imports ibapi.order_cancel", "subclasses EClient",
                   "constructs EClient", ".placeOrder", ".cancelOrder", ".exerciseOptions"):
        assert needle in joined, needle


def test_legacy_exception_list_is_accurate():
    for rel in LEGACY_ECLIENT_EXCEPTIONS:
        assert (ROOT / rel).exists(), f"{rel} no longer exists; remove it from LEGACY_ECLIENT_EXCEPTIONS"


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
        "       hermes.market.orderbook\n"
        "bad = [m for m in sys.modules if m == 'ibapi' or m.startswith('ibapi.')]\n"
        "assert not bad, bad\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=ROOT)
