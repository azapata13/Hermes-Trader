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
    max_update_age_ms: int = 5000
    transient_grace_ms: int = 250
    bbo_tolerance_ticks: int = 0
    bbo_mismatch_grace_ms: int = 1000
    escalate_after_ms: int = 3000
    require_bbo_confirmation: bool = True


@dataclass(frozen=True, slots=True)
class HermesConfig:
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    ibkr: IbkrConfig = field(default_factory=IbkrConfig)
    instrument: InstrumentConfig = field(default_factory=InstrumentConfig)
    book: BookConfig = field(default_factory=BookConfig)


_SECTIONS: dict[str, type] = {
    "safety": SafetyConfig,
    "ibkr": IbkrConfig,
    "instrument": InstrumentConfig,
    "book": BookConfig,
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
    if b.max_update_age_ms == 0:
        raise ConfigError("[book].max_update_age_ms must be > 0")
    if b.escalate_after_ms < max(b.transient_grace_ms, b.bbo_mismatch_grace_ms):
        raise ConfigError("[book].escalate_after_ms must be >= transient_grace_ms and bbo_mismatch_grace_ms")


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
