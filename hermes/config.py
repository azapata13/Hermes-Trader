"""Hermès configuration: TOML file -> frozen, validated dataclasses.

Design rules
------------
* stdlib only (``tomllib``, Python 3.11+).
* Unknown sections/keys are rejected (typos must not silently fall back to defaults).
* Types are checked strictly (``bool`` is never accepted where ``int`` is expected and vice versa).
* Phase C safety invariants are enforced at load time:
  ``safety.orders_enabled`` must be ``false`` and ``ibkr.read_only`` must be ``true``.
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "hermes.toml"


class ConfigError(ValueError):
    """Raised when configuration is missing, malformed or violates a safety invariant."""


@dataclass(frozen=True, slots=True)
class SafetyConfig:
    orders_enabled: bool = False


@dataclass(frozen=True, slots=True)
class IbkrConfig:
    host: str = "127.0.0.1"
    port: int = 7496
    client_id: int = 110
    connect_timeout_s: float = 8.0
    read_only: bool = True
    heartbeat_interval_ms: int = 1000


@dataclass(frozen=True, slots=True)
class InstrumentConfig:
    instrument_id: int = 1
    symbol: str = "MNQ"
    sec_type: str = "FUT"
    exchange: str = "CME"
    currency: str = "USD"
    trading_class: str = "MNQ"
    last_trade_date_or_contract_month: str = "202612"


@dataclass(frozen=True, slots=True)
class BookConfig:
    depth_rows: int = 10
    min_valid_rows: int = 5
    settle_ms: int = 500
    max_update_age_ms: int = 0          # 0 = disabled: silence alone is not a failure (C3 amendment B)
    transient_grace_ms: int = 250
    bbo_tolerance_ticks: int = 0
    bbo_mismatch_grace_ms: int = 1000
    escalate_after_ms: int = 3000
    require_bbo_confirmation: bool = True


@dataclass(frozen=True, slots=True)
class SubscriptionsConfig:
    tick_by_tick_all_last: bool = True
    tick_by_tick_bid_ask: bool = True      # primary BBO source (required for a VALID book by default)
    l1_market_data: bool = True            # reqMktData: LIVE confirmation + health cross-check only
    depth_smart: bool = False              # isSmartDepth for reqMktDepth (CME: False)


@dataclass(frozen=True, slots=True)
class SessionConfig:
    contract_timeout_s: float = 15.0
    market_rule_timeout_s: float = 15.0
    clock_tick_interval_ms: int = 250      # emitted only when a callback arrives (not a precise timer)
    resync_min_interval_s: float = 5.0     # depth resubscribe pacing
    resync_max_per_window: int = 5
    resync_window_s: float = 300.0
    conflict_retry_interval_s: float = 30.0  # 10197 recovery pacing
    conflict_max_attempts: int = 3
    recovery_attempt_timeout_s: float = 20.0  # an attempt that has not PROVEN recovery by then has failed
    reconnect_initial_backoff_s: float = 2.0
    reconnect_max_backoff_s: float = 60.0
    shutdown_grace_s: float = 1.0


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    # Conservative global safety guard; NOT a model of IBKR pacing (endpoint limits may be added later).
    max_requests_per_second: float = 10.0
    burst: int = 20
    first_req_id: int = 10_000


@dataclass(frozen=True, slots=True)
class RecorderConfig:
    enabled: bool = True
    directory: str = "~/hermes-data/recordings"
    ring_capacity: int = 200_000
    batch_max: int = 5_000
    flush_interval_ms: int = 200
    rotate_minutes: int = 60


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    report_interval_s: float = 10.0
    log_directory: str = "~/hermes-data/logs"
    snapshot_interval_ms: int = 100
    console: bool = True


@dataclass(frozen=True, slots=True)
class TapeConfig:
    max_trades: int = 50_000               # tape bound by count
    max_age_s: float = 1800.0              # tape bound by age (event time)
    quote_history: int = 64                # BidAsk states kept for quote-move ambiguity checks
    ambiguity_window_ms: int = 50          # PROVISIONAL C4 baseline (not optimized); recalibrate on real sessions
    max_quote_age_ms: int = 0              # 0 = disabled (a quiet BBO is legitimate)
    snapshot_trades: int = 20              # latest N trades exposed per snapshot
    confidence_direct_quote: float = 1.0          # deterministic RANKS, not probabilities
    confidence_historical_quote: float = 0.6
    confidence_tick_rule: float = 0.3
    allowed_special_conditions: str = ""   # comma separated; others => UNKNOWN(INELIGIBLE)
    classify_past_limit: bool = False
    classify_unreported: bool = False


@dataclass(frozen=True, slots=True)
class HermesConfig:
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    ibkr: IbkrConfig = field(default_factory=IbkrConfig)
    instrument: InstrumentConfig = field(default_factory=InstrumentConfig)
    book: BookConfig = field(default_factory=BookConfig)
    subscriptions: SubscriptionsConfig = field(default_factory=SubscriptionsConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    recorder: RecorderConfig = field(default_factory=RecorderConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    tape: TapeConfig = field(default_factory=TapeConfig)


_SECTIONS: dict[str, type] = {
    "safety": SafetyConfig,
    "ibkr": IbkrConfig,
    "instrument": InstrumentConfig,
    "book": BookConfig,
    "subscriptions": SubscriptionsConfig,
    "session": SessionConfig,
    "gateway": GatewayConfig,
    "recorder": RecorderConfig,
    "telemetry": TelemetryConfig,
    "tape": TapeConfig,
}


def _check_type(section: str, key: str, value: Any, expected: type) -> Any:
    where = f"[{section}].{key}"
    if expected is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where} must be a boolean, got {type(value).__name__}")
        return value
    if expected is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where} must be an integer, got {type(value).__name__}")
        return value
    if expected is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where} must be a number, got {type(value).__name__}")
        return float(value)
    if expected is str:
        if not isinstance(value, str):
            raise ConfigError(f"{where} must be a string, got {type(value).__name__}")
        return value
    raise ConfigError(f"{where}: unsupported config type {expected!r}")  # pragma: no cover


def _build_section(name: str, cls: type, raw: Mapping[str, Any]) -> Any:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"[{name}] must be a table")
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = sorted(set(raw) - set(fields))
    if unknown:
        raise ConfigError(f"[{name}] unknown key(s): {', '.join(unknown)}")
    # With `from __future__ import annotations` field types are strings; map them explicitly.
    type_map = {"bool": bool, "int": int, "float": float, "str": str}
    kwargs = {}
    for key, value in raw.items():
        expected = type_map[fields[key].type]  # type: ignore[index]
        kwargs[key] = _check_type(name, key, value, expected)
    return cls(**kwargs)


def _validate(cfg: HermesConfig) -> None:
    # ---- Phase C safety invariants (non-negotiable) ----
    if cfg.safety.orders_enabled:
        raise ConfigError("[safety].orders_enabled must be false in Phase C (no order execution path exists)")
    if not cfg.ibkr.read_only:
        raise ConfigError("[ibkr].read_only must be true in Phase C")

    # ---- sanity ----
    if not (0 < cfg.ibkr.port < 65536):
        raise ConfigError("[ibkr].port out of range")
    if cfg.ibkr.client_id < 0:
        raise ConfigError("[ibkr].client_id must be >= 0")
    if cfg.ibkr.client_id == 0:
        # clientId 0 can bind manual TWS orders (reqAutoOpenOrders); never used by Hermès.
        raise ConfigError("[ibkr].client_id 0 is reserved and must not be used by Hermès")
    if cfg.ibkr.connect_timeout_s <= 0:
        raise ConfigError("[ibkr].connect_timeout_s must be > 0")
    if cfg.ibkr.heartbeat_interval_ms <= 0:
        raise ConfigError("[ibkr].heartbeat_interval_ms must be > 0")
    if cfg.instrument.instrument_id <= 0:
        raise ConfigError("[instrument].instrument_id must be > 0")

    b = cfg.book
    if b.depth_rows < 1:
        raise ConfigError("[book].depth_rows must be >= 1")
    if not (1 <= b.min_valid_rows <= b.depth_rows):
        raise ConfigError("[book].min_valid_rows must be in [1, depth_rows]")
    for name in ("settle_ms", "max_update_age_ms", "transient_grace_ms",
                 "bbo_tolerance_ticks", "bbo_mismatch_grace_ms", "escalate_after_ms"):
        if getattr(b, name) < 0:
            raise ConfigError(f"[book].{name} must be >= 0")
    if b.escalate_after_ms < max(b.transient_grace_ms, b.bbo_mismatch_grace_ms):
        raise ConfigError("[book].escalate_after_ms must be >= transient_grace_ms and bbo_mismatch_grace_ms")


    s = cfg.session
    for name in ("contract_timeout_s", "market_rule_timeout_s", "resync_min_interval_s", "resync_window_s",
                 "conflict_retry_interval_s", "recovery_attempt_timeout_s", "reconnect_initial_backoff_s",
                 "reconnect_max_backoff_s"):
        if getattr(s, name) <= 0:
            raise ConfigError(f"[session].{name} must be > 0")
    if s.shutdown_grace_s < 0:
        raise ConfigError("[session].shutdown_grace_s must be >= 0")
    if s.clock_tick_interval_ms <= 0:
        raise ConfigError("[session].clock_tick_interval_ms must be > 0")
    if s.resync_max_per_window < 1 or s.conflict_max_attempts < 1:
        raise ConfigError("[session] retry budgets must be >= 1")
    if s.reconnect_max_backoff_s < s.reconnect_initial_backoff_s:
        raise ConfigError("[session].reconnect_max_backoff_s must be >= reconnect_initial_backoff_s")

    g = cfg.gateway
    if g.max_requests_per_second <= 0 or g.burst < 1:
        raise ConfigError("[gateway] rate limit must be positive")
    if g.first_req_id < 1:
        raise ConfigError("[gateway].first_req_id must be >= 1")

    r = cfg.recorder
    if r.ring_capacity < 16 or r.batch_max < 1 or r.flush_interval_ms < 1 or r.rotate_minutes < 1:
        raise ConfigError("[recorder] sizes/intervals out of range (ring_capacity >= 16)")
    if not r.directory.strip():
        raise ConfigError("[recorder].directory must not be empty")

    t = cfg.telemetry
    if t.report_interval_s <= 0 or t.snapshot_interval_ms < 1:
        raise ConfigError("[telemetry] intervals must be > 0")

    tp = cfg.tape
    if tp.max_trades < 1 or tp.max_age_s <= 0 or tp.quote_history < 2 or tp.snapshot_trades < 0:
        raise ConfigError("[tape] bounds out of range (max_trades >= 1, max_age_s > 0, quote_history >= 2)")
    if tp.ambiguity_window_ms < 0 or tp.max_quote_age_ms < 0:
        raise ConfigError("[tape] windows must be >= 0")
    if not (1.0 >= tp.confidence_direct_quote >= tp.confidence_historical_quote >= tp.confidence_tick_rule > 0.0):
        raise ConfigError("[tape] confidences must satisfy 1 >= quote >= quote_history >= tick_rule > 0")

    sub = cfg.subscriptions
    if cfg.book.require_bbo_confirmation and not sub.tick_by_tick_bid_ask:
        raise ConfigError("[book].require_bbo_confirmation needs [subscriptions].tick_by_tick_bid_ask = true")


def config_from_mapping(data: Mapping[str, Any]) -> HermesConfig:
    unknown = sorted(set(data) - set(_SECTIONS))
    if unknown:
        raise ConfigError(f"unknown config section(s): {', '.join(unknown)}")
    sections = {name: _build_section(name, cls, data.get(name, {})) for name, cls in _SECTIONS.items()}
    cfg = HermesConfig(**sections)
    _validate(cfg)
    return cfg


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> HermesConfig:
    p = Path(path)
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {p}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {p}: {exc}") from exc
    return config_from_mapping(data)
