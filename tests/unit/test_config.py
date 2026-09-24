from __future__ import annotations

import pytest

from hermes.config import ConfigError, config_from_mapping, load_config


def test_default_file_loads():
    cfg = load_config()
    assert cfg.instrument.symbol == "MNQ"
    assert cfg.book.depth_rows == 10
    assert cfg.book.min_valid_rows == 5
    assert cfg.book.require_bbo_confirmation is True


def test_empty_mapping_gives_safe_defaults():
    cfg = config_from_mapping({})
    assert cfg.safety.orders_enabled is False
    assert cfg.ibkr.read_only is True


def test_orders_enabled_is_rejected():
    with pytest.raises(ConfigError, match="orders_enabled"):
        config_from_mapping({"safety": {"orders_enabled": True}})


def test_read_only_false_is_rejected():
    with pytest.raises(ConfigError, match="read_only"):
        config_from_mapping({"ibkr": {"read_only": False}})


def test_client_id_zero_is_rejected():
    with pytest.raises(ConfigError, match="client_id"):
        config_from_mapping({"ibkr": {"client_id": 0}})


@pytest.mark.parametrize("data", [
    {"bogus": {}},
    {"book": {"depth_rowz": 10}},
])
def test_unknown_keys_rejected(data):
    with pytest.raises(ConfigError, match="unknown"):
        config_from_mapping(data)


@pytest.mark.parametrize("data", [
    {"book": {"depth_rows": True}},          # bool is not int
    {"book": {"depth_rows": "10"}},
    {"safety": {"orders_enabled": 0}},       # int is not bool
    {"ibkr": {"host": 127}},
])
def test_strict_types(data):
    with pytest.raises(ConfigError):
        config_from_mapping(data)


def test_float_accepts_int():
    assert config_from_mapping({"ibkr": {"connect_timeout_s": 5}}).ibkr.connect_timeout_s == 5.0


@pytest.mark.parametrize("book", [
    {"depth_rows": 0},
    {"min_valid_rows": 0},
    {"depth_rows": 5, "min_valid_rows": 6},
    {"settle_ms": -1},
    {"max_update_age_ms": -1},
    {"escalate_after_ms": 100, "transient_grace_ms": 250},
])
def test_book_validation(book):
    with pytest.raises(ConfigError):
        config_from_mapping({"book": book})


def test_configurable_depth():
    cfg = config_from_mapping({"book": {"depth_rows": 20, "min_valid_rows": 3}})
    assert (cfg.book.depth_rows, cfg.book.min_valid_rows) == (20, 3)


def test_missing_and_invalid_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("[book\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(bad)


def test_config_is_frozen():
    cfg = load_config()
    with pytest.raises(Exception):
        cfg.book.depth_rows = 3  # type: ignore[misc]


def test_update_age_disabled_by_default():
    # C3 amendment B: silence alone is not a failure; stream age is telemetry.
    assert load_config().book.max_update_age_ms == 0


def test_c3_sections_defaults():
    cfg = load_config()
    assert cfg.subscriptions.tick_by_tick_all_last and cfg.subscriptions.tick_by_tick_bid_ask
    assert cfg.subscriptions.l1_market_data and not cfg.subscriptions.depth_smart
    assert cfg.session.conflict_retry_interval_s == 30.0 and cfg.session.conflict_max_attempts == 3
    assert cfg.recorder.directory == "~/hermes-data/recordings"
    assert cfg.gateway.max_requests_per_second <= 10


@pytest.mark.parametrize("data", [
    {"session": {"conflict_max_attempts": 0}},
    {"session": {"resync_min_interval_s": 0}},
    {"session": {"reconnect_initial_backoff_s": 10.0, "reconnect_max_backoff_s": 5.0}},
    {"gateway": {"max_requests_per_second": 0}},
    {"recorder": {"ring_capacity": 4}},
    {"recorder": {"directory": " "}},
    {"telemetry": {"report_interval_s": 0}},
    {"subscriptions": {"tick_by_tick_bid_ask": False}},   # BBO confirmation still required
])
def test_c3_validation(data):
    with pytest.raises(ConfigError):
        config_from_mapping(data)


def test_bbo_optional_only_when_confirmation_disabled():
    cfg = config_from_mapping({"subscriptions": {"tick_by_tick_bid_ask": False},
                               "book": {"require_bbo_confirmation": False}})
    assert not cfg.subscriptions.tick_by_tick_bid_ask


def test_tape_defaults_and_validation():
    t = load_config().tape
    assert (t.max_trades, t.max_age_s, t.max_quote_age_ms) == (50_000, 1800.0, 0)
    assert t.confidence_direct_quote > t.confidence_historical_quote > t.confidence_tick_rule > 0
    for bad in ({"max_trades": 0}, {"max_age_s": 0}, {"quote_history": 1}, {"ambiguity_window_ms": -1},
                {"confidence_tick_rule": 0.9}, {"confidence_direct_quote": 1.5}, {"snapshot_trades": -1}):
        with pytest.raises(ConfigError):
            config_from_mapping({"tape": bad})
