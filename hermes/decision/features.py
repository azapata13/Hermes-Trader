"""V1 Pro D7 — explainable setup characteristics for historical comparison. Pure, deterministic.

A setup is described by a fixed, versioned set of categorical characteristics read from the
structured C9 candidate (never from free-text details, never from a model). Two setups are
COMPARABLE when every field of ``COMPARABLE_FIELDS`` is equal: no distance, no weights, no
learned similarity, so every comparison can be explained field by field.
"""

from __future__ import annotations

from hermes.decision.candidate import SetupCandidate

FEATURES_VERSION = 1
# Fields that must be EQUAL for two setups to be comparable (similarity definition v1).
COMPARABLE_FIELDS = ("direction", "regime_5m", "setup_1m", "trigger_30s", "vwap", "tape", "session")
RISK_BUCKETS_PT = (6.0, 10.0, 14.0)          # risk <= 6 / <= 10 / <= 14 / > 14 points


def _vote(c: SetupCandidate, name: str) -> str:
    for comp in c.orderflow_evidence:
        if comp.name == name:
            v = comp.vote.value
            if v in ("LONG", "SHORT"):
                return "with" if v == c.direction.value else "against"
            return "neutral" if v == "NEUTRAL" else "unavailable"
    return "unavailable"


def _vwap(c: SetupCandidate) -> str:
    want = "mid_above_rth_vwap" if c.direction.value == "LONG" else "mid_below_rth_vwap"
    for cond in c.regime_5m.conditions:
        if cond.name == want:
            return {"pass": "with", "fail": "against"}.get(cond.status, "unavailable")
    return "unavailable"


def _risk_bucket(points: float | None) -> str:
    if points is None:
        return "unavailable"
    for limit in RISK_BUCKETS_PT:
        if points <= limit:
            return f"<={limit:g}pt"
    return f">{RISK_BUCKETS_PT[-1]:g}pt"


def _time_bucket(end_s: int | None) -> str:
    if end_s is None:
        return "unavailable"
    minute = (end_s % 86400) // 60
    half = (minute // 30) * 30
    return f"{half // 60:02d}:{half % 60:02d}Z"           # 30-minute UTC bucket (no timezone guess)


def setup_features(c: SetupCandidate) -> tuple[tuple[str, str], ...]:
    """Categorical characteristics (all strings), stable order."""
    return (
        ("features_version", str(FEATURES_VERSION)),
        ("direction", c.direction.value),
        ("regime_5m", c.regime_5m.result),
        ("setup_1m", c.setup_1m.result),
        ("trigger_30s", c.trigger_30s.result),
        ("vwap", _vwap(c)),
        ("tape", _vote(c, "trade_flow")),
        ("ofi", _vote(c, "ofi")),
        ("sweep", _vote(c, "sweep_follow")),
        ("session", "RTH" if c.session_rth else "ETH"),
        ("time_bucket", _time_bucket(c.trigger_bar_end_s)),
        ("risk_bucket", _risk_bucket(c.risk_points)),
    )


def record_features(rec) -> tuple[tuple[str, str], ...]:
    return setup_features(rec.candidate)


def comparable_key(features: dict | tuple) -> tuple[str, ...] | None:
    """The equality key of the similarity definition, or None if a required field is missing."""
    f = dict(features)
    if str(f.get("features_version")) != str(FEATURES_VERSION):
        return None
    key = tuple(str(f.get(k, "")) for k in COMPARABLE_FIELDS)
    if any(not v or v == "unavailable" for v in key):
        return None
    return key


def describe_key(key: tuple[str, ...]) -> str:
    return ", ".join(f"{k}={v}" for k, v in zip(COMPARABLE_FIELDS, key))
