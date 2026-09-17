#!/usr/bin/env python3
"""Currency-safe report scopes with explicit dated FX provenance."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from enum import Enum
import json
from pathlib import Path
import sys
from typing import Iterable, Mapping, Sequence

from renderer.portfolio_valuation import AccountValuation, HoldingValuation, ValuationResult, build_valuation
from renderer.portfolio_xml_model import (
    CanonicalEvent,
    NormalizationResult,
    PortfolioModel,
    TransferStatus,
    normalize_transactions,
)


class CurrencyViewMode(str, Enum):
    CONSOLIDATED = "CONSOLIDATED"
    SEPARATE_BY_CURRENCY = "SEPARATE_BY_CURRENCY"
    BOTH = "BOTH"


class ConversionStatus(str, Enum):
    EXACT = "EXACT"
    CARRIED_FORWARD = "CARRIED_FORWARD"
    MISSING = "MISSING"
    NOT_REQUIRED = "NOT_REQUIRED"
    PARTIAL = "PARTIAL"


@dataclass(frozen=True)
class ReportingCurrency:
    code: str

    def __post_init__(self) -> None:
        normalized = self.code.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError(f"reporting currency must be a 3-letter ISO code: {self.code!r}")
        object.__setattr__(self, "code", normalized)


@dataclass(frozen=True)
class PortfolioScope:
    portfolio_uuids: tuple[str, ...]


@dataclass(frozen=True)
class CurrencyScope:
    currency: str
    security_uuids: tuple[str, ...]
    account_uuids: tuple[str, ...]
    portfolio_scope: PortfolioScope
    transaction_event_ids: tuple[str, ...]
    holding_dimension: str = "SECURITY_CURRENCY"
    account_dimension: str = "ACCOUNT_CURRENCY"
    transaction_dimension: str = "ANY_MONETARY_OR_SECURITY_COMPONENT_CURRENCY"


@dataclass(frozen=True)
class FxRate:
    source_currency: str
    reporting_currency: str
    rate_date: date
    rate: Decimal
    source: str
    inverse_of: str | None = None


@dataclass(frozen=True)
class ConvertedAmount:
    original_value: Decimal
    original_currency: str
    reporting_currency: str
    conversion_date: date
    converted_value: Decimal | None
    status: ConversionStatus
    fx_rate: Decimal | None
    fx_rate_date: date | None
    fx_source: str | None
    label: str
    reason: str | None = None


@dataclass(frozen=True)
class AggregateAmount:
    reporting_currency: str
    complete_value: Decimal | None
    available_value: Decimal
    status: ConversionStatus
    components: tuple[ConvertedAmount, ...]
    missing_labels: tuple[str, ...]


@dataclass(frozen=True)
class OriginalSeriesPoint:
    day: date
    currency: str
    value: Decimal
    source: str


@dataclass(frozen=True)
class ConvertedSeriesPoint:
    day: date
    aggregate: AggregateAmount


@dataclass(frozen=True)
class ReturnSeriesPoint:
    day: date
    cumulative_return: Decimal
    currency_scope: str
    source: str


@dataclass(frozen=True)
class ComparativeReturnSeries:
    unit: str
    series_by_currency: Mapping[str, tuple[ReturnSeriesPoint, ...]]


@dataclass(frozen=True)
class ExternalFlowResult:
    currency: str
    total: Decimal
    external_event_ids: tuple[str, ...]
    excluded_linked_transfer_ids: tuple[str, ...]
    uncertain_event_ids: tuple[str, ...]
    status: ConversionStatus


@dataclass(frozen=True)
class CurrencyView:
    currency: str
    scope: CurrencyScope
    holdings: tuple[HoldingValuation, ...]
    accounts: tuple[AccountValuation, ...]
    events: tuple[CanonicalEvent, ...]
    original_market_value: Decimal
    original_cash_balance: Decimal
    original_total_value: Decimal
    market_value_series: tuple[OriginalSeriesPoint, ...]
    return_series: tuple[ReturnSeriesPoint, ...]
    performance_status: ConversionStatus
    performance_reason: str | None
    external_flows: ExternalFlowResult


@dataclass(frozen=True)
class ConsolidatedView:
    reporting_currency: str
    total_value: AggregateAmount
    market_value_series: tuple[ConvertedSeriesPoint, ...]
    performance_status: ConversionStatus
    performance_reason: str | None


@dataclass(frozen=True)
class ReportViews:
    mode: CurrencyViewMode
    reporting_currency: ReportingCurrency
    consolidated: ConsolidatedView | None
    per_currency: Mapping[str, CurrencyView]
    comparative_returns: ComparativeReturnSeries | None

    def audit_summary(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "reporting_currency": self.reporting_currency.code,
            "currencies": sorted(self.per_currency),
            "per_currency": {
                code: {
                    "holdings": len(view.holdings),
                    "accounts": len(view.accounts),
                    "events": len(view.events),
                    "original_market_value": str(view.original_market_value),
                    "original_cash_balance": str(view.original_cash_balance),
                    "original_total_value": str(view.original_total_value),
                    "performance_status": view.performance_status.value,
                    "external_flow_status": view.external_flows.status.value,
                }
                for code, view in sorted(self.per_currency.items())
            },
            "consolidated": None
            if self.consolidated is None
            else {
                "status": self.consolidated.total_value.status.value,
                "complete_value": _decimal_text(self.consolidated.total_value.complete_value),
                "available_value": str(self.consolidated.total_value.available_value),
                "missing_labels": list(self.consolidated.total_value.missing_labels),
                "performance_status": self.consolidated.performance_status.value,
            },
            "comparative_unit": self.comparative_returns.unit if self.comparative_returns else None,
        }


class DatedFxProvider:
    """Lookup exact/latest-prior FX without pre-first-date or 1.0 fallback."""

    def __init__(self, rates: Iterable[FxRate] = ()):
        grouped: dict[tuple[str, str], list[FxRate]] = {}
        for item in rates:
            if item.rate <= 0 or not item.rate.is_finite():
                raise ValueError(f"FX rate must be positive and finite: {item}")
            source = item.source_currency.upper()
            target = item.reporting_currency.upper()
            normalized = FxRate(source, target, item.rate_date, item.rate, item.source, item.inverse_of)
            grouped.setdefault((source, target), []).append(normalized)
        self._rates = {
            key: tuple(sorted(items, key=lambda item: item.rate_date))
            for key, items in grouped.items()
        }

    def lookup(self, source: str, target: str, requested: date) -> FxRate | None:
        source = source.upper()
        target = target.upper()
        if source == target:
            return None
        resolved = self._direct_or_inverse(source, target, requested)
        if resolved is not None:
            return resolved
        if source == "EUR" or target == "EUR":
            return None
        source_to_eur = self._direct_or_inverse(source, "EUR", requested)
        eur_to_target = self._direct_or_inverse("EUR", target, requested)
        if source_to_eur is None or eur_to_target is None:
            return None
        with localcontext() as context:
            context.prec = 28
            rate = source_to_eur.rate * eur_to_target.rate
        return FxRate(
            source,
            target,
            min(source_to_eur.rate_date, eur_to_target.rate_date),
            rate,
            f"cross-via-EUR:{source_to_eur.source};{eur_to_target.source}",
            inverse_of=None,
        )

    def _direct_or_inverse(
        self,
        source: str,
        target: str,
        requested: date,
    ) -> FxRate | None:
        direct = self._latest(self._rates.get((source, target), ()), requested)
        if direct is not None:
            return direct
        inverse = self._latest(self._rates.get((target, source), ()), requested)
        if inverse is None:
            return None
        with localcontext() as context:
            context.prec = 28
            rate = Decimal(1) / inverse.rate
        return FxRate(
            source,
            target,
            inverse.rate_date,
            rate,
            f"inverse:{inverse.source}",
            inverse_of=f"{target}/{source}",
        )

    @staticmethod
    def _latest(items: Sequence[FxRate], requested: date) -> FxRate | None:
        eligible = [item for item in items if item.rate_date <= requested]
        return max(eligible, key=lambda item: item.rate_date) if eligible else None


def convert_amount(
    value: Decimal,
    source_currency: str,
    reporting_currency: ReportingCurrency | str,
    conversion_date: date,
    provider: DatedFxProvider,
    *,
    label: str,
) -> ConvertedAmount:
    source = source_currency.upper()
    reporting = (
        reporting_currency.code
        if isinstance(reporting_currency, ReportingCurrency)
        else ReportingCurrency(reporting_currency).code
    )
    if source == reporting:
        return ConvertedAmount(
            value,
            source,
            reporting,
            conversion_date,
            value,
            ConversionStatus.NOT_REQUIRED,
            None,
            None,
            "identity",
            label,
        )
    rate = provider.lookup(source, reporting, conversion_date)
    if rate is None:
        return ConvertedAmount(
            value,
            source,
            reporting,
            conversion_date,
            None,
            ConversionStatus.MISSING,
            None,
            None,
            None,
            label,
            f"no {source}/{reporting} FX rate on or before {conversion_date.isoformat()}",
        )
    status = (
        ConversionStatus.EXACT
        if rate.rate_date == conversion_date
        else ConversionStatus.CARRIED_FORWARD
    )
    return ConvertedAmount(
        value,
        source,
        reporting,
        conversion_date,
        value * rate.rate,
        status,
        rate.rate,
        rate.rate_date,
        rate.source,
        label,
    )


def aggregate_amounts(
    components: Iterable[tuple[Decimal, str, date, str]],
    reporting_currency: ReportingCurrency | str,
    provider: DatedFxProvider,
) -> AggregateAmount:
    reporting = (
        reporting_currency
        if isinstance(reporting_currency, ReportingCurrency)
        else ReportingCurrency(reporting_currency)
    )
    converted = tuple(
        convert_amount(value, currency, reporting, day, provider, label=label)
        for value, currency, day, label in components
    )
    available = sum(
        (item.converted_value for item in converted if item.converted_value is not None),
        Decimal(0),
    )
    missing = tuple(item.label for item in converted if item.status == ConversionStatus.MISSING)
    if missing:
        status = ConversionStatus.PARTIAL if len(missing) < len(converted) else ConversionStatus.MISSING
        complete = None
    elif any(item.status == ConversionStatus.CARRIED_FORWARD for item in converted):
        status = ConversionStatus.CARRIED_FORWARD
        complete = available
    elif any(item.status == ConversionStatus.EXACT for item in converted):
        status = ConversionStatus.EXACT
        complete = available
    else:
        status = ConversionStatus.NOT_REQUIRED
        complete = available
    return AggregateAmount(reporting.code, complete, available, status, converted, missing)


def consolidate_value_series(
    points: Iterable[OriginalSeriesPoint],
    reporting_currency: ReportingCurrency | str,
    provider: DatedFxProvider,
) -> tuple[ConvertedSeriesPoint, ...]:
    grouped: dict[date, list[OriginalSeriesPoint]] = {}
    for point in points:
        grouped.setdefault(point.day, []).append(point)
    return tuple(
        ConvertedSeriesPoint(
            day,
            aggregate_amounts(
                (
                    (point.value, point.currency, point.day, f"{point.source}:{point.currency}")
                    for point in grouped[day]
                ),
                reporting_currency,
                provider,
            ),
        )
        for day in sorted(grouped)
    )


def external_cash_flows(events: Iterable[CanonicalEvent], currency: str) -> ExternalFlowResult:
    currency = currency.upper()
    total = Decimal(0)
    external_ids = []
    linked_ids = []
    uncertain_ids = []
    for event in events:
        impacts = [impact for impact in event.cash_impacts if impact.currency == currency]
        if not impacts:
            continue
        if event.transfer_status == TransferStatus.LINKED:
            linked_ids.append(event.event_id)
            continue
        if event.transfer_status == TransferStatus.UNCERTAIN_NOTE:
            uncertain_ids.append(event.event_id)
        if event.canonical_type not in {"DEPOSIT", "REMOVAL", "TRANSFER_IN", "TRANSFER_OUT", "TRANSFER"}:
            continue
        external_ids.append(event.event_id)
        total += sum((impact.amount.value for impact in impacts), Decimal(0))
    return ExternalFlowResult(
        currency,
        total,
        tuple(external_ids),
        tuple(linked_ids),
        tuple(uncertain_ids),
        ConversionStatus.PARTIAL if uncertain_ids else ConversionStatus.NOT_REQUIRED,
    )


def build_comparative_return_series(
    series_by_currency: Mapping[str, Sequence[ReturnSeriesPoint]],
) -> ComparativeReturnSeries | None:
    nonempty = {
        code.upper(): tuple(points)
        for code, points in series_by_currency.items()
        if points
    }
    if len(nonempty) < 2:
        return None
    for code, points in nonempty.items():
        if any(point.currency_scope != code for point in points):
            raise ValueError(f"return series scope mismatch for {code}")
    return ComparativeReturnSeries("PERCENT", dict(sorted(nonempty.items())))


def build_currency_views(
    model: PortfolioModel,
    valuation: ValuationResult | None = None,
    normalized: NormalizationResult | None = None,
    *,
    mode: CurrencyViewMode = CurrencyViewMode.CONSOLIDATED,
    reporting_currency: ReportingCurrency | str | None = None,
    fx_provider: DatedFxProvider | None = None,
    market_series_by_currency: Mapping[str, Sequence[OriginalSeriesPoint]] | None = None,
    return_series_by_currency: Mapping[str, Sequence[ReturnSeriesPoint]] | None = None,
) -> ReportViews:
    normalized = normalized or normalize_transactions(model)
    valuation = valuation or build_valuation(model, normalized)
    reporting = (
        reporting_currency
        if isinstance(reporting_currency, ReportingCurrency)
        else ReportingCurrency(reporting_currency or model.base_currency)
    )
    provider = fx_provider or DatedFxProvider()
    market_series_by_currency = market_series_by_currency or {}
    return_series_by_currency = return_series_by_currency or {}

    currencies = sorted(
        {model.base_currency}
        | {item.currency for item in model.accounts.values()}
        | {item.currency for item in model.securities.values()}
        | {impact.currency for event in normalized.events for impact in event.cash_impacts}
        | {unit.currency for event in normalized.events for unit in event.units}
    )
    views: dict[str, CurrencyView] = {}
    for currency in currencies:
        holdings = tuple(item for item in valuation.holdings if item.security_currency == currency)
        accounts = tuple(item for item in valuation.accounts if item.currency == currency)
        security_uuids = {item.security_uuid for item in holdings}
        account_uuids = {item.account_uuid for item in accounts}
        events = tuple(
            event
            for event in normalized.events
            if currency in {impact.currency for impact in event.cash_impacts}
            or currency in {unit.currency for unit in event.units}
            or bool(security_uuids.intersection(event.security_uuids))
        )
        market_value = sum(
            (item.original_market_value or Decimal(0) for item in holdings), Decimal(0)
        )
        cash_balance = sum((item.balance.value for item in accounts), Decimal(0))
        return_points = tuple(return_series_by_currency.get(currency, ()))
        performance_status = (
            ConversionStatus.NOT_REQUIRED if return_points else ConversionStatus.MISSING
        )
        performance_reason = None if return_points else "validated scoped valuation/cash-flow return series unavailable"
        scope = CurrencyScope(
            currency,
            tuple(sorted(security_uuids)),
            tuple(sorted(account_uuids)),
            PortfolioScope(tuple(sorted({item.portfolio_uuid for item in holdings}))),
            tuple(event.event_id for event in events),
        )
        views[currency] = CurrencyView(
            currency,
            scope,
            holdings,
            accounts,
            events,
            market_value,
            cash_balance,
            market_value + cash_balance,
            tuple(market_series_by_currency.get(currency, ())),
            return_points,
            performance_status,
            performance_reason,
            external_cash_flows(events, currency),
        )

    consolidated = None
    if mode in {CurrencyViewMode.CONSOLIDATED, CurrencyViewMode.BOTH}:
        components = []
        for holding in valuation.holdings:
            if holding.original_market_value is not None:
                components.append(
                    (
                        holding.original_market_value,
                        holding.security_currency,
                        valuation.valuation_date,
                        f"holding:{holding.portfolio_uuid}:{holding.security_uuid}",
                    )
                )
        for account in valuation.accounts:
            components.append(
                (
                    account.balance.value,
                    account.currency,
                    valuation.valuation_date,
                    f"account:{account.account_uuid}",
                )
            )
        all_market_points = [
            point for points in market_series_by_currency.values() for point in points
        ]
        converted_series = consolidate_value_series(all_market_points, reporting, provider)
        series_missing = bool(all_market_points) and any(
            point.aggregate.status in {ConversionStatus.MISSING, ConversionStatus.PARTIAL}
            for point in converted_series
        )
        consolidated = ConsolidatedView(
            reporting.code,
            aggregate_amounts(components, reporting, provider),
            converted_series,
            ConversionStatus.PARTIAL if series_missing else ConversionStatus.MISSING,
            "validated consolidated valuation/cash-flow performance series unavailable"
            if not all_market_points
            else "one or more dated FX conversions are unavailable"
            if series_missing
            else "consolidated monetary series exists; return series still requires validated cash flows",
        )

    comparative = (
        build_comparative_return_series(return_series_by_currency)
        if mode == CurrencyViewMode.BOTH
        else None
    )
    return ReportViews(mode, reporting, consolidated, dict(sorted(views.items())), comparative)


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit currency-safe Portfolio Performance views")
    parser.add_argument("--audit", required=True, type=Path, metavar="XML")
    parser.add_argument("--mode", choices=[item.value for item in CurrencyViewMode], default="BOTH")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        model = PortfolioModel.from_path(args.audit)
        views = build_currency_views(model, mode=CurrencyViewMode(args.mode))
    except (OSError, ValueError) as error:
        sys.stderr.write(f"Currency view audit failed: {error}\n")
        return 2
    sys.stdout.write(json.dumps(views.audit_summary(), ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
