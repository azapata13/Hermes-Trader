"""V1 Pro D8 — structured, verifiable context for the LLM (Sol) and a guard on what it says back.

Architecture (one direction only)::

    deterministic engine (facts, safety, lifecycle, outcome, history, similarity)
        -> build_context()  : versioned JSON, every number computed by the engine
        -> render_prompt()  : the ONLY input the LLM sees
        -> LLM text         : interpretation / explanation
        -> guard_output()   : display-only lines (numbers must exist in the context, no instructions)

The LLM never receives a callable, a handle, a config or a credential, and its output is plain text
that is only ever displayed. It cannot place or modify an order, change SafetyPolicy, read_only,
orders_enabled or the approval mode, approve anything, turn STALE into ACTIVE, or invent an entry,
a stop, a statistic or a history: a line containing a number that is not in the context, or an
instruction-like phrase, is dropped and counted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from hermes.analytics.stats import HISTORY_MIN_SAMPLES, OUTCOME_DEFINITION, Comparable

CONTEXT_SCHEMA = "hermes-llm-context/1"
MAX_OUTPUT_LINES = 4
AUTHORITY = (
    "You interpret facts computed by a deterministic engine. You have no broker access and no "
    "execution authority. You cannot place, modify or cancel orders, approve a trade, change any "
    "setting, re-activate an expired entry, or invent prices, stops, statistics or history. Use only "
    "the numbers in this context. Do not tell the human to enter or exit."
)
_REQUIRED = {
    "schema": str, "execution": str, "authority": str,
    "setup": dict, "outcome": dict, "structure": dict, "history": dict,
}
_SETUP_REQUIRED = ("setup_id", "direction", "original_entry", "original_stop", "risk_pt", "age_s", "phase")
_OUTCOME_REQUIRED = ("status", "mfe_pt", "mae_pt", "stop_first", "plus_1r", "plus_2r")
_BANNED = re.compile(
    r"\b(place|send|submit|cancel|modify)\s+(an?\s+)?(order|trade)|\border(s)?_enabled\b|\bread_only\b|"
    r"\bplaceorder\b|\b(buy|sell|enter|short|long)\s+(now|here|immediately)\b|\bclick\s+enter\b|"
    r"\bmove\s+(the\s+)?stop\b|\bset\s+(the\s+)?stop\b|\bnew\s+entry\s+(at|is)\b|\bapprove(d)?\s+the\s+trade\b|"
    r"\bignore\s+(the\s+)?(safety|rules|previous)\b|\bdisable\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"(?<![\w.])[-+]?\d+(?:[.,]\d+)?%?")
_ALLOWED_TOKENS = {"5m", "1m", "30s", "1r", "2r", "3r", "+1r", "+2r", "+3r", "vwap", "ofi"}


def _pt(view, units):
    return None if units is None else view.points(units)


def build_context(view, comparable: Comparable | None, now_wall_ns: int) -> dict:
    """Engine facts only. ``view`` is a tracking SetupView; ``comparable`` the D7 comparison (or None)."""
    o = view.outcome
    f = dict(view.features)
    r = o.r_wall_ns
    if comparable is not None and comparable.displayable:
        s = comparable.stats
        history = {
            "available": True, "samples": s.samples, "execution": "SHADOW",
            "plus_1r_rate": round(s.plus_1r / s.samples, 4), "plus_2r_rate": round(s.plus_2r / s.samples, 4),
            "stop_first_rate": round(s.stop_first / s.samples, 4),
            "median_mfe_pt": s.median_mfe_pt, "median_mae_pt": s.median_mae_pt,
            "median_result_10m_pt": s.median_result_10m_pt,
            "outcome_definition": OUTCOME_DEFINITION, "selection_definition": comparable.selection_definition,
            "source": comparable.source,
        }
    else:
        n = comparable.stats.samples if comparable is not None and comparable.stats is not None else 0
        history = {"available": False, "samples": n,
                   "reason": f"fewer than {HISTORY_MIN_SAMPLES} comparable recorded setups"}
    return {
        "schema": CONTEXT_SCHEMA,
        "execution": "SHADOW",
        "authority": AUTHORITY,
        "setup": {
            "setup_id": view.setup_id, "direction": view.direction, "symbol": view.symbol,
            "original_entry": _pt(view, view.original_entry_units), "original_stop": _pt(view, view.original_stop_units),
            "risk_pt": view.risk_points, "age_s": max(0, (now_wall_ns - view.created_wall_ns) // 1_000_000_000),
            "phase": view.phase.value, "window_close_status": view.window_close_status,
            "entry_window_open": view.phase.value == "ACTIVE",
            "rearm_of": view.rearm_of, "rearmed_by": view.rearmed_by,
            "new_entry_after_rearm": _pt(view, view.rearm_entry_units),
            "fresh_trigger_required": view.phase.value in ("MONITORING", "REARMED"),
        },
        "outcome": {
            "status": o.status.value, "last_price": _pt(view, o.last_price_units),
            "mfe_pt": _pt(view, o.mfe_units), "mae_pt": _pt(view, o.mae_units),
            "stop_first": bool(o.stop_touched and not (r and r[0] is not None)),
            "plus_1r": bool(r and r[0] is not None), "plus_2r": bool(len(r) > 1 and r[1] is not None),
            "incomplete_reason": o.incomplete_reason,
        },
        "structure": {k: f.get(k, "unavailable") for k in ("regime_5m", "setup_1m", "trigger_30s", "vwap", "tape",
                                                            "ofi", "sweep", "session")},
        "history": history,
    }


def validate_context(ctx) -> list[str]:
    """Problems that make a context unusable (the LLM is then NOT called)."""
    if not isinstance(ctx, dict):
        return ["context is not an object"]
    problems = []
    for k, t in _REQUIRED.items():
        if not isinstance(ctx.get(k), t):
            problems.append(f"missing or invalid {k}")
    if problems:
        return problems
    if ctx["schema"] != CONTEXT_SCHEMA:
        problems.append(f"unsupported schema {ctx['schema']}")
    if ctx["execution"] != "SHADOW":
        problems.append("V1 contexts describe shadow setups only")
    for k in _SETUP_REQUIRED:
        if ctx["setup"].get(k) is None:
            problems.append(f"setup.{k} is null")
    for k in _OUTCOME_REQUIRED:
        if k not in ctx["outcome"]:
            problems.append(f"outcome.{k} missing")
    s, h, o = ctx["setup"], ctx["history"], ctx["outcome"]
    if s.get("entry_window_open") and s.get("phase") != "ACTIVE":
        problems.append("contradiction: entry window open but phase is not ACTIVE")
    if s.get("phase") == "ACTIVE" and s.get("window_close_status"):
        problems.append("contradiction: ACTIVE with a closed window")
    if o.get("stop_first") and o.get("plus_1r"):
        problems.append("contradiction: stop-first and +1R")
    if h.get("available") and (not isinstance(h.get("samples"), int) or h["samples"] < HISTORY_MIN_SAMPLES):
        problems.append(f"history marked available with fewer than {HISTORY_MIN_SAMPLES} samples")
    return problems


def render_prompt(ctx: dict) -> str:
    return (f"{AUTHORITY}\n\nTask: in at most {MAX_OUTPUT_LINES} short lines, explain what the facts say about "
            "this setup (still aligned or not, why the entry window closed, whether a fresh 30s trigger is "
            "needed, what the comparable history shows if available). Quote numbers only from the context.\n\n"
            "CONTEXT (JSON):\n" + json.dumps(ctx, sort_keys=True, ensure_ascii=False))


def _context_numbers(obj, out: set[float]) -> set[float]:
    if isinstance(obj, bool) or obj is None:
        return out
    if isinstance(obj, (int, float)):
        out.add(round(float(obj), 2))
        out.add(round(float(obj) * 100, 0))             # rates may be quoted as percentages
    elif isinstance(obj, dict):
        for v in obj.values():
            _context_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _context_numbers(v, out)
    elif isinstance(obj, str):
        for m in _NUMBER.findall(obj):
            try:
                out.add(round(float(m.rstrip("%").replace(",", ".")), 2))
            except ValueError:
                pass
    return out


@dataclass(slots=True)
class Guarded:
    lines: list[str] = field(default_factory=list)
    dropped_numbers: int = 0
    dropped_instructions: int = 0
    truncated: int = 0

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def guard_output(text, ctx: dict) -> Guarded:
    """Keep only display-safe interpretation lines. Never raises; non-text output yields nothing."""
    g = Guarded()
    if not isinstance(text, str):
        return g
    known = _context_numbers(ctx, set())
    for raw in text.splitlines():
        line = raw.strip().lstrip("-•* ").strip()
        if not line:
            continue
        if _BANNED.search(line):
            g.dropped_instructions += 1
            continue
        bad = False
        for m in _NUMBER.finditer(line):
            tok = m.group(0)
            tail = line[m.end():m.end() + 1].lower()
            if (tok + tail).lower().lstrip("+") in _ALLOWED_TOKENS or (tok.lower() + tail) in _ALLOWED_TOKENS:
                continue
            try:
                val = round(abs(float(tok.rstrip("%").replace(",", "."))), 2)
            except ValueError:
                bad = True
                break
            if val not in known and -val not in known and val != 0:
                bad = True
                break
        if bad:
            g.dropped_numbers += 1
            continue
        if len(g.lines) >= MAX_OUTPUT_LINES:
            g.truncated += 1
            continue
        g.lines.append(line[:240])
    return g


def interpret(ctx: dict, llm_fn) -> Guarded:
    """Validate -> prompt -> LLM (injected callable returning text) -> guard. Any failure: nothing shown."""
    if validate_context(ctx):
        return Guarded()
    try:
        out = llm_fn(render_prompt(ctx))
    except Exception:  # noqa: BLE001 - the LLM is optional and never on the safety path
        return Guarded()
    return guard_output(out, ctx)
