"""Contract resolution and PriceGrid initialization (pure, deterministic logic).

Used by the Normalizer so that replay reproduces contract resolution exactly from the
recorded raw stream. The live session only issues the requests.

Rules
-----
* ``reqContractDetails`` must yield EXACTLY ONE contract matching the requested spec
  (symbol, secType, currency, tradingClass, contract month, exchange). Zero or several
  matches is a hard failure — Hermès never guesses.
* The market rule for the trading exchange is selected positionally
  (``validExchanges[i] <-> marketRuleIds[i]``); the grid is built from ``minTick`` AND the rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hermes.ibkr.market_rules import MarketRuleError, market_rule_id_for_exchange
from hermes.ibkr.raw_events import RawContractDetails
from hermes.market.pricegrid import PriceGrid, PriceGridError


class ContractResolutionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ContractSpec:
    symbol: str
    sec_type: str
    exchange: str
    currency: str
    trading_class: str
    month: str                      # YYYYMM or YYYYMMDD

    def to_params(self) -> tuple[tuple[str, str], ...]:
        return (("symbol", self.symbol), ("sec_type", self.sec_type), ("exchange", self.exchange),
                ("currency", self.currency), ("trading_class", self.trading_class), ("month", self.month))

    @classmethod
    def from_params(cls, params: tuple[tuple[str, str], ...]) -> "ContractSpec":
        d = dict(params)
        try:
            return cls(symbol=d["symbol"], sec_type=d["sec_type"], exchange=d["exchange"],
                       currency=d["currency"], trading_class=d["trading_class"], month=d["month"])
        except KeyError as exc:
            raise ContractResolutionError(f"contract request params missing {exc}") from exc


@dataclass(frozen=True, slots=True)
class ResolvedContract:
    con_id: int
    symbol: str
    local_symbol: str
    exchange: str
    expiry: str
    multiplier: str
    min_tick: float
    market_rule_id: int
    time_zone: str
    trading_hours: str
    liquid_hours: str


def matches(spec: ContractSpec, d: RawContractDetails) -> bool:
    exchanges = {e.strip().upper() for e in d.valid_exchanges.split(",") if e.strip()}
    return (
        d.symbol.upper() == spec.symbol.upper()
        and d.sec_type.upper() == spec.sec_type.upper()
        and d.currency.upper() == spec.currency.upper()
        and d.trading_class.upper() == spec.trading_class.upper()
        and d.last_trade_date_or_contract_month.startswith(spec.month)
        and (d.exchange.upper() == spec.exchange.upper() or spec.exchange.upper() in exchanges)
    )


@dataclass(slots=True)
class PendingContract:
    instrument_id: int
    spec: ContractSpec
    details: list[RawContractDetails] = field(default_factory=list)

    def resolve(self) -> ResolvedContract:
        found = [d for d in self.details if matches(self.spec, d)]
        if not found:
            raise ContractResolutionError(
                f"no contract matches {self.spec} ({len(self.details)} candidate(s) returned)")
        con_ids = {d.con_id for d in found}
        if len(con_ids) != 1:
            raise ContractResolutionError(
                f"ambiguous contract: {len(con_ids)} distinct matches {sorted(con_ids)} for {self.spec}")
        d = found[0]
        if d.min_tick <= 0:
            raise ContractResolutionError(f"invalid minTick {d.min_tick}")
        try:
            rule_id = market_rule_id_for_exchange(d.valid_exchanges, d.market_rule_ids, self.spec.exchange)
        except MarketRuleError as exc:
            raise ContractResolutionError(f"market rule selection failed: {exc}") from exc
        return ResolvedContract(
            con_id=d.con_id, symbol=d.symbol, local_symbol=d.local_symbol, exchange=self.spec.exchange,
            expiry=d.last_trade_date_or_contract_month, multiplier=d.multiplier, min_tick=d.min_tick,
            market_rule_id=rule_id, time_zone=d.time_zone_id, trading_hours=d.trading_hours,
            liquid_hours=d.liquid_hours,
        )


def build_price_grid(contract: ResolvedContract, increments: tuple[tuple[float, float], ...]) -> PriceGrid:
    try:
        return PriceGrid.from_market_rule(contract.min_tick, increments)
    except PriceGridError as exc:
        raise ContractResolutionError(f"invalid market rule {contract.market_rule_id}: {exc}") from exc
