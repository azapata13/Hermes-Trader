from __future__ import annotations

import pytest

from hermes.ibkr.contracts import ContractResolutionError, ContractSpec, PendingContract
from hermes.ibkr.errors import SUBSCRIPTION_FATAL, classify_error
from hermes.market.events import ErrorClass as E
from tests.support import SPEC, RawScript


@pytest.mark.parametrize("code,cls", [
    (317, E.DEPTH_RESET), (1100, E.CONNECTIVITY_LOST), (1101, E.RESTORED_DATA_LOST),
    (1102, E.RESTORED_DATA_KEPT), (2103, E.FARM_BROKEN), (2104, E.FARM_OK), (2106, E.INFO),
    (2158, E.INFO), (2110, E.SERVER_CONNECTIVITY_BROKEN), (309, E.CAPACITY_EXCEEDED),
    (354, E.SUBSCRIPTION_REJECTED), (10167, E.DATA_NOT_LIVE), (10197, E.SESSION_CONFLICT),
    (200, E.CONTRACT_ERROR), (300, E.INFO), (2150, E.INFO), (99999, E.UNKNOWN), (321, E.SUBSCRIPTION_REJECTED),
])
def test_classification(code, cls):
    assert classify_error(code) is cls


def test_readonly_rejection_detected_by_message():
    assert classify_error(321, "The API interface is currently in Read-Only mode.") is E.READONLY_REJECTED


def test_unknown_is_fail_safe_for_subscriptions():
    assert E.UNKNOWN in SUBSCRIPTION_FATAL and E.INFO not in SUBSCRIPTION_FATAL


def test_spec_params_roundtrip():
    assert ContractSpec.from_params(SPEC.to_params()) == SPEC
    with pytest.raises(ContractResolutionError):
        ContractSpec.from_params((("symbol", "MNQ"),))


def test_resolution_exactly_one():
    s = RawScript()
    p = PendingContract(1, SPEC, [s.contract_details()])
    c = p.resolve()
    assert (c.con_id, c.market_rule_id, c.local_symbol) == (770561201, 67, "MNQZ6")
    # same conId reported twice (e.g. once per exchange listing) is still exactly one contract
    p = PendingContract(1, SPEC, [s.contract_details(), s.contract_details(exchange="QBALGO")])
    assert p.resolve().con_id == 770561201


@pytest.mark.parametrize("over", [
    {"trading_class": "NQ"}, {"currency": "EUR"}, {"last_trade_date_or_contract_month": "20270319"},
    {"sec_type": "FOP"}, {"exchange": "X", "valid_exchanges": "X"},
])
def test_resolution_mismatch(over):
    with pytest.raises(ContractResolutionError):
        PendingContract(1, SPEC, [RawScript().contract_details(**over)]).resolve()


def test_resolution_bad_rules_or_tick():
    with pytest.raises(ContractResolutionError):
        PendingContract(1, SPEC, [RawScript().contract_details(market_rule_ids="67")]).resolve()
    with pytest.raises(ContractResolutionError):
        PendingContract(1, SPEC, [RawScript().contract_details(min_tick=0.0)]).resolve()
