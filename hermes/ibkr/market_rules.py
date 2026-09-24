"""Selection of the IBKR market rule that applies to a given exchange.

``ContractDetails.marketRuleIds`` is a comma-separated list positionally aligned with
``ContractDetails.validExchanges`` (IBKR documentation). The rule for the exchange we trade
on is fetched with ``reqMarketRule(id)`` and fed to ``PriceGrid.from_market_rule``.
"""

from __future__ import annotations


class MarketRuleError(ValueError):
    pass


def market_rule_id_for_exchange(valid_exchanges: str, market_rule_ids: str, exchange: str) -> int:
    exchanges = [e.strip() for e in valid_exchanges.split(",") if e.strip()]
    rule_ids = [r.strip() for r in market_rule_ids.split(",") if r.strip()]
    if not exchanges or not rule_ids:
        raise MarketRuleError("contract has no validExchanges or marketRuleIds")
    if len(exchanges) != len(rule_ids):
        raise MarketRuleError(
            f"validExchanges ({len(exchanges)}) and marketRuleIds ({len(rule_ids)}) are not aligned"
        )
    target = exchange.strip().upper()
    matches = [rule_ids[i] for i, e in enumerate(exchanges) if e.upper() == target]
    if not matches:
        raise MarketRuleError(f"exchange {exchange!r} not in validExchanges {exchanges}")
    if len(set(matches)) != 1:
        raise MarketRuleError(f"conflicting market rules for {exchange!r}: {matches}")
    try:
        return int(matches[0])
    except ValueError as exc:
        raise MarketRuleError(f"non-integer market rule id {matches[0]!r}") from exc
