"""C9e — one structured reason shape for the decision layer and the human-approval payload.

Every reason is ``Reason(source, code, detail, severity)``:

* ``source``   where it comes from (a fixed vocabulary, ``SOURCES``);
* ``code``     stable, machine-testable snake_case identifier (``is_code``);
* ``detail``   free human-readable evidence (never parsed by code);
* ``severity`` BLOCK > HOLD > CAUTION > SUPPORT > INFO.

Reasons are evidence, not instructions: nothing here can remove a restriction. A BLOCK or HOLD
reason always comes from a deterministic rule (SafetyPolicy, lifecycle, approval gate or the
candidate rules); SUPPORT/CAUTION/INFO never gate anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

REASONS_SCHEMA_VERSION = 1


class Severity(str, Enum):
    BLOCK = "BLOCK"          # prevents approval / made the candidate NONE or BLOCKED
    HOLD = "HOLD"            # temporary: not approvable at this observation, candidate not ended
    CAUTION = "CAUTION"      # informational warning for the human; never gates
    SUPPORT = "SUPPORT"      # evidence in favour; never sufficient on its own
    INFO = "INFO"            # neutral facts


SEVERITY_RANK = {Severity.BLOCK: 0, Severity.HOLD: 1, Severity.CAUTION: 2, Severity.SUPPORT: 3, Severity.INFO: 4}

# creation-time (candidate rules)
SRC_SAFETY = "safety"            # SafetyPolicy at candidate creation
SRC_TIMING = "timing"            # trigger bar availability / evaluation lag
SRC_REGIME_5M = "regime_5m"
SRC_SETUP_1M = "setup_1m"
SRC_TRIGGER_30S = "trigger_30s"
SRC_BAR_QUALITY = "bar_quality"
SRC_SESSION = "session"
SRC_ORDERFLOW = "orderflow"
SRC_RISK = "risk"
# after creation
SRC_LIFECYCLE = "lifecycle"      # status transitions / holds while the candidate was ACTIONABLE
SRC_APPROVAL = "approval"        # approval-time gate + approval-time SafetyPolicy codes
SRC_RESPONSE = "response"        # human response consistency / future-execution prerequisites (pure)

SOURCES = frozenset({SRC_SAFETY, SRC_TIMING, SRC_REGIME_5M, SRC_SETUP_1M, SRC_TRIGGER_30S, SRC_BAR_QUALITY,
                     SRC_SESSION, SRC_ORDERFLOW, SRC_RISK, SRC_LIFECYCLE, SRC_APPROVAL, SRC_RESPONSE})

_CODE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


def is_code(code: str) -> bool:
    """Stable machine code: non-empty snake_case ``[a-z0-9][a-z0-9_]*``."""
    return bool(code) and code[0] != "_" and set(code) <= _CODE_CHARS


@dataclass(frozen=True, slots=True)
class Reason:
    source: str
    code: str
    detail: str
    severity: Severity

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"unknown reason source {self.source!r}")
        if not isinstance(self.code, str) or not is_code(self.code):
            raise ValueError(f"reason code must be snake_case: {self.code!r}")
        if not isinstance(self.severity, Severity):
            raise ValueError(f"severity must be a Severity: {self.severity!r}")


def ordered(reasons) -> tuple[Reason, ...]:
    """Deterministic presentation order: by severity, stable within a severity; exact duplicates
    removed (first occurrence kept)."""
    uniq = tuple(dict.fromkeys(reasons))
    return tuple(sorted(uniq, key=lambda r: SEVERITY_RANK[r.severity]))


def codes(reasons, *, severity: Severity | None = None, source: str | None = None) -> tuple[str, ...]:
    return tuple(r.code for r in reasons
                 if (severity is None or r.severity is severity) and (source is None or r.source == source))
