#!/usr/bin/env python3
"""Build versioned, presentation-only reporting-currency client modes."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_DOWN
import hashlib
import json
import re
from typing import Any, Mapping
import unicodedata

from renderer.detailed_report import OfficialInputs, decimal_text
from renderer.ecb_rates import EcbRateCache
from renderer.portfolio_valuation import ValuationResult
from renderer.portfolio_xml_model import (
    NormalizationResult,
    PortfolioModel,
    TaxonomyNode,
)


CLIENT_VIEWS_SCHEMA_VERSION = "1.0"
RECONCILIATION_TOLERANCE = Decimal("0.01")
RETURN_TOLERANCE = Decimal("1e-12")
CENT = Decimal("0.01")
HALF_CENT = CENT / Decimal(2)
ACTION_LABELS = {
    "BUY": "Покупка",
    "SELL": "Продажа",
    "DEPOSIT": "Пополнение",
    "REMOVAL": "Вывод средств",
    "DIVIDENDS": "Дивиденды",
    "INTEREST": "Процентный доход",
    "INTEREST_CHARGE": "Процентное списание",
    "FEES": "Комиссия",
    "TAXES": "Налог",
    "TRANSFER_IN": "Перевод средств",
    "TRANSFER_OUT": "Перевод средств",
    "TRANSFER": "Перевод средств",
}
REGIONS_BY_STABLE_KEY = {
    "r10": ("Европа", "europe"),
    "r20": ("Канада и Латинская Америка", "canada_latam"),
    "r30": ("Азия и Тихоокеанский регион", "asia_pacific"),
    "r40": ("Африка", "africa"),
    "r50": ("Азия и Тихоокеанский регион", "asia_pacific"),
}
REGION_NAME_ALIASES = {
    "сша": ("США", "usa"),
    "usa": ("США", "usa"),
    "united states": ("США", "usa"),
    "америки без сша": ("Канада и Латинская Америка", "canada_latam"),
    "americas usa": ("Канада и Латинская Америка", "canada_latam"),
    "americas without usa": ("Канада и Латинская Америка", "canada_latam"),
    "europe": ("Европа", "europe"),
    "европа": ("Европа", "europe"),
    "asia": ("Азия и Тихоокеанский регион", "asia_pacific"),
    "азия": ("Азия и Тихоокеанский регион", "asia_pacific"),
    "oceania": ("Азия и Тихоокеанский регион", "asia_pacific"),
    "океания": ("Азия и Тихоокеанский регион", "asia_pacific"),
    "africa": ("Африка", "africa"),
    "африка": ("Африка", "africa"),
    "multiregional": ("Глобальные инструменты", "global"),
    "cross region": ("Глобальные инструменты", "global"),
    "межрегиональный": ("Глобальные инструменты", "global"),
    "global": ("Глобальные инструменты", "global"),
    "глобальный": ("Глобальные инструменты", "global"),
}
COUNTRY_NAMES_RU = {
    "US": "США",
    "CA": "Канада",
    "BR": "Бразилия",
    "DK": "Дания",
    "GB": "Великобритания",
}
COUNTRY_REGION_BY_ISO = {
    "US": ("США", "usa"),
    "CA": ("Канада и Латинская Америка", "canada_latam"),
    "BR": ("Канада и Латинская Америка", "canada_latam"),
    "DK": ("Европа", "europe"),
    "GB": ("Европа", "europe"),
}
COUNTRY_NAME_ALIASES = {
    "сша": ("США", "us"),
    "usa": ("США", "us"),
    "united states": ("США", "us"),
    "канада": ("Канада", "ca"),
    "canada": ("Канада", "ca"),
    "бразилия": ("Бразилия", "br"),
    "brazil": ("Бразилия", "br"),
    "дания": ("Дания", "dk"),
    "denmark": ("Дания", "dk"),
    "великобритания": ("Великобритания", "gb"),
    "united kingdom": ("Великобритания", "gb"),
    "great britain": ("Великобритания", "gb"),
}
CLIENT_VIEW_REASON_CODES = frozenset(
    {
        "CV_ACTIVE_CURRENCY_EVIDENCE_MISSING",
        "CV_ACTIVE_CURRENCY_INVALID",
        "CV_ALL_TIME_ENDPOINT_MISMATCH",
        "CV_BASE_CURRENCY_MISMATCH",
        "CV_BASE_OVERVIEW_MISSING",
        "CV_CASH_CURRENCY_INVALID",
        "CV_CASH_REPORTING_CURRENCY_MISMATCH",
        "CV_CASH_ROWS_MISSING",
        "CV_CASH_SCOPE_MISMATCH",
        "CV_CASH_TOTAL_MISMATCH",
        "CV_CASH_VALUE_INVALID",
        "CV_CROSS_MODE_CASH_MISMATCH",
        "CV_CROSS_MODE_SCOPE_MISMATCH",
        "CV_EXPOSURE_CURRENCY_INVALID",
        "CV_EXPOSURE_TOTAL_MISMATCH",
        "CV_EXPOSURE_TOTAL_NONPOSITIVE",
        "CV_EXPOSURE_VALUE_INVALID",
        "CV_FX_PROVENANCE_MISSING",
        "CV_HOLDING_CURRENCY_MISMATCH",
        "CV_HOLDING_IDENTITY_MISMATCH",
        "CV_HOLDINGS_MISSING",
        "CV_MODE_DATE_MISMATCH",
        "CV_MODE_DUPLICATE",
        "CV_MODE_KEY_MISMATCH",
        "CV_MODES_EMPTY",
        "CV_PERIOD_ANCHOR_MISMATCH",
        "CV_PERIOD_BASE_NEGATIVE_100",
        "CV_PERIOD_EMPTY",
        "CV_PERIOD_ENDPOINT_MISMATCH",
        "CV_PORTFOLIO_TOTAL_MISMATCH",
        "CV_REPORTING_CURRENCY_INVALID",
        "CV_SCOPE_ACCOUNT_MISMATCH",
        "CV_SCOPE_CASH_EVIDENCE_MISSING",
        "CV_SCOPE_CASH_ORIGINAL_CURRENCY_MISMATCH",
        "CV_SCOPE_CASH_REPORTING_CURRENCY_MISMATCH",
        "CV_SCOPE_CASH_TOTAL_MISMATCH",
        "CV_SCOPE_CURRENCY_INVALID",
        "CV_SCOPE_EVIDENCE_MISSING",
        "CV_SCOPE_HOLDING_CURRENCY_MISMATCH",
        "CV_SCOPE_HOLDING_MISMATCH",
        "CV_SCOPE_INVENTORY_EMPTY",
        "CV_SCOPE_NOT_FULL_CLIENT",
        "CV_SCOPE_OVERVIEW_MISSING",
        "CV_SCOPE_PORTFOLIO_MISMATCH",
        "CV_SCOPE_SOURCE_CURRENCY_MISMATCH",
        "CV_SERIES_DATES_MISMATCH",
        "CV_SERIES_EMPTY",
        "CV_SERIES_VALUE_MISMATCH",
    }
)


class ClientViewError(ValueError):
    """A client mode cannot be built without violating its financial contract."""

    def __init__(self, code: str, message: str):
        if code not in CLIENT_VIEW_REASON_CODES:
            raise ValueError(f"unknown client-view reason code: {code}")
        self.code = code
        super().__init__(message)


def component_rounding_tolerance(component_count: int) -> Decimal:
    """Bound the sum of cent-rounded components against one rounded aggregate.

    Each component can contribute at most half a cent of rounding error, and the
    independently rounded aggregate can contribute one additional half cent.
    """

    if component_count < 0:
        raise ValueError("component count cannot be negative")
    return HALF_CENT * Decimal(component_count + 1)


def _money(value: Decimal, source: str, target: str, day: date, cache: EcbRateCache) -> Decimal:
    if source == target:
        return value.quantize(CENT, rounding=ROUND_HALF_DOWN)
    resolution = cache.resolve(source, target, day)
    return (value * resolution.rate).quantize(CENT, rounding=ROUND_HALF_DOWN)


def _classification_map(
    model: PortfolioModel, taxonomy_key: str
) -> dict[str, str]:
    taxonomy = next(
        (
            item
            for item in model.taxonomies
            if ("portfolioClassificationKey", taxonomy_key) in item.root.data
        ),
        None,
    )
    if taxonomy is None:
        return {}
    answer: dict[str, str] = {}

    def visit(node: TaxonomyNode, top_level: str) -> None:
        for assignment in node.assignments:
            if assignment.security_uuid is not None and assignment.weight.raw > 0:
                answer.setdefault(assignment.security_uuid, top_level)
        for child in node.children:
            visit(child, top_level)

    for child in taxonomy.root.children:
        visit(child, child.name)
    return answer


def _classification_path_map(
    model: PortfolioModel, taxonomy_key: str
) -> dict[str, tuple[str, ...]]:
    """Return the complete confirmed taxonomy path for each assigned security."""

    taxonomy = next(
        (
            item
            for item in model.taxonomies
            if ("portfolioClassificationKey", taxonomy_key) in item.root.data
        ),
        None,
    )
    if taxonomy is None:
        return {}
    answer: dict[str, tuple[str, ...]] = {}

    def visit(node: TaxonomyNode, path: tuple[str, ...]) -> None:
        current_path = (*path, node.name)
        for assignment in node.assignments:
            if assignment.security_uuid is not None and assignment.weight.raw > 0:
                answer.setdefault(assignment.security_uuid, current_path)
        for child in node.children:
            visit(child, current_path)

    for child in taxonomy.root.children:
        visit(child, ())
    return answer


def _normalized_taxonomy_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))


def _taxonomy_data(node: TaxonomyNode) -> dict[str, str]:
    return {key.casefold(): value.strip() for key, value in node.data}


def _country_from_node(node: TaxonomyNode) -> tuple[str, str] | None:
    data = _taxonomy_data(node)
    iso = data.get("iso3166-1-alpha-2", "").upper()
    classification_key = data.get("portfolioclassificationkey", "")
    if not iso and classification_key.casefold().startswith("country_"):
        iso = classification_key.split("_", 1)[1].upper()
    if re.fullmatch(r"[A-Z]{2}", iso):
        return COUNTRY_NAMES_RU.get(iso, node.name), iso.casefold()
    return COUNTRY_NAME_ALIASES.get(_normalized_taxonomy_label(node.name))


def _region_from_node(node: TaxonomyNode) -> tuple[str, str] | None:
    data = _taxonomy_data(node)
    classification_key = data.get("portfolioclassificationkey", "").casefold()
    if classification_key in REGIONS_BY_STABLE_KEY:
        return REGIONS_BY_STABLE_KEY[classification_key]
    country = _country_from_node(node)
    if country is not None:
        iso = country[1].upper()
        if iso in COUNTRY_REGION_BY_ISO:
            return COUNTRY_REGION_BY_ISO[iso]
    return REGION_NAME_ALIASES.get(_normalized_taxonomy_label(node.name))


def _geography_classification_map(
    model: PortfolioModel,
) -> dict[str, tuple[tuple[str, str], tuple[str, str] | None]]:
    """Resolve PP geography by stable keys first and localized names second."""

    taxonomy = next(
        (
            item
            for item in model.taxonomies
            if ("portfolioClassificationKey", "regions") in item.root.data
        ),
        None,
    )
    if taxonomy is None:
        return {}
    answer: dict[str, tuple[tuple[str, str], tuple[str, str] | None]] = {}

    def visit(
        node: TaxonomyNode,
        region: tuple[str, str] | None,
        country: tuple[str, str] | None,
    ) -> None:
        current_region = region or _region_from_node(node)
        current_country = _country_from_node(node) or country
        if current_region is None and current_country is not None:
            current_region = COUNTRY_REGION_BY_ISO.get(current_country[1].upper())
        resolved_region = current_region or ("Другие регионы", "other")
        for assignment in node.assignments:
            if assignment.security_uuid is not None and assignment.weight.raw > 0:
                answer.setdefault(
                    assignment.security_uuid,
                    (resolved_region, current_country),
                )
        for child in node.children:
            visit(child, current_region, current_country)

    for child in taxonomy.root.children:
        visit(child, None, None)
    return answer


def _grouped_allocation(
    scoped_holdings: list[Any],
    values_by_security: Mapping[str, Decimal],
    classifications: Mapping[str, str],
    liquidity: Decimal,
    total: Decimal,
) -> list[dict[str, str]]:
    grouped: dict[str, Decimal] = {}
    for item in scoped_holdings:
        value = values_by_security[item.security_uuid]
        name = classifications.get(item.security_uuid, "Прочее")
        grouped[name] = grouped.get(name, Decimal(0)) + value
    grouped["Свободные средства"] = grouped.get(
        "Свободные средства", Decimal(0)
    ) + liquidity
    return [
        {
            "name": name,
            "value": decimal_text(value),
            "share": decimal_text(value / total if total else Decimal(0)),
        }
        for name, value in sorted(
            grouped.items(), key=lambda item: (-item[1], item[0].casefold())
        )
        if value != 0
    ]


def _geography_allocation(
    scoped_holdings: list[Any],
    values_by_security: Mapping[str, Decimal],
    classifications: Mapping[
        str, tuple[tuple[str, str], tuple[str, str] | None]
    ],
) -> list[dict[str, Any]]:
    """Build region totals plus confirmed country and instrument drill-down data."""

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in scoped_holdings:
        (name, region_key), country = classifications.get(
            item.security_uuid,
            (("Другие регионы", "other"), None),
        )
        key = (name, region_key)
        region = grouped.setdefault(
            key,
            {"value": Decimal(0), "countries": {}, "instruments": []},
        )
        value = values_by_security[item.security_uuid]
        instrument = {
            "name": item.security_name,
            "instrument_currency": item.security_currency,
            "value": value,
        }
        region["value"] += value
        if region_key == "global":
            region["instruments"].append(instrument)
            continue

        if country is None:
            region["instruments"].append(instrument)
            continue
        country_name, country_key = country
        country_row = region["countries"].setdefault(
            (country_name, country_key), {"value": Decimal(0), "instruments": []}
        )
        country_row["value"] += value
        country_row["instruments"].append(instrument)

    invested_total = sum(
        (region["value"] for region in grouped.values()), Decimal(0)
    )

    def instrument_rows(items: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [
            {
                "name": str(item["name"]),
                "instrument_currency": str(item["instrument_currency"]),
                "value": decimal_text(item["value"]),
                "share": decimal_text(
                    item["value"] / invested_total if invested_total else Decimal(0)
                ),
            }
            for item in sorted(
                items, key=lambda row: (-row["value"], str(row["name"]).casefold())
            )
            if item["value"] != 0
        ]

    rows: list[dict[str, Any]] = []
    for (name, region_key), region in sorted(
        grouped.items(), key=lambda item: (-item[1]["value"], item[0][0].casefold())
    ):
        value = region["value"]
        if value == 0:
            continue
        countries = [
            {
                "name": country_name,
                "country_key": country_key,
                "value": decimal_text(country["value"]),
                "share": decimal_text(
                    country["value"] / invested_total
                    if invested_total
                    else Decimal(0)
                ),
                "instruments": instrument_rows(country["instruments"]),
            }
            for (country_name, country_key), country in sorted(
                region["countries"].items(),
                key=lambda item: (-item[1]["value"], item[0][0].casefold()),
            )
            if country["value"] != 0
        ]
        rows.append(
            {
                "name": name,
                "region_key": region_key,
                "value": decimal_text(value),
                "share": decimal_text(
                    value / invested_total if invested_total else Decimal(0)
                ),
                "countries": countries,
                "instruments": instrument_rows(region["instruments"]),
            }
        )
    return rows


def _currency_exposure(
    holdings: list[Mapping[str, str]],
    cash_accounts: list[Mapping[str, Any]],
    official_total: Decimal,
) -> list[dict[str, str]]:
    """Return whole-portfolio exposure by each asset's original currency."""

    grouped: dict[str, Decimal] = {}

    def add(currency_value: Any, reporting_value: Any, source: str) -> None:
        currency = str(currency_value).upper()
        if re.fullmatch(r"[A-Z]{3}", currency) is None:
            raise ClientViewError(
                "CV_EXPOSURE_CURRENCY_INVALID",
                f"invalid {source} currency in client exposure",
            )
        try:
            value = Decimal(str(reporting_value))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ClientViewError(
                "CV_EXPOSURE_VALUE_INVALID",
                f"invalid {source} value in client exposure",
            ) from error
        if value != 0:
            grouped[currency] = grouped.get(currency, Decimal(0)) + value

    for holding in holdings:
        add(holding["instrument_currency"], holding["value"], "instrument")
    for account in cash_accounts:
        add(account["original_currency"], account["reporting_value"], "cash")

    grouped = {
        currency: value for currency, value in grouped.items() if value != 0
    }
    if official_total <= 0 or not grouped:
        raise ClientViewError(
            "CV_EXPOSURE_TOTAL_NONPOSITIVE",
            "whole-portfolio currency exposure has no positive total",
        )
    grouped_total = sum(grouped.values(), Decimal(0))
    tolerance = component_rounding_tolerance(len(holdings) + len(cash_accounts))
    if abs(grouped_total - official_total) > tolerance:
        raise ClientViewError(
            "CV_EXPOSURE_TOTAL_MISMATCH",
            "whole-portfolio currency exposure does not reconcile to official total"
        )
    return [
        {
            "currency": currency,
            "value": decimal_text(value),
            "share": decimal_text(value / grouped_total),
        }
        for currency, value in sorted(grouped.items())
    ]


def _scope_source_currencies(
    scoped_holdings: list[Any],
    accounts: tuple[Any, ...],
    scope_accounts: set[str],
) -> set[str]:
    """Include non-zero cash currencies as well as instrument currencies."""

    currencies = {str(item.security_currency).upper() for item in scoped_holdings}
    currencies.update(
        str(account.currency).upper()
        for account in accounts
        if account.account_uuid in scope_accounts and account.balance.value != 0
    )
    invalid = sorted(
        code for code in currencies if re.fullmatch(r"[A-Z]{3}", code) is None
    )
    if invalid:
        raise ClientViewError(
            "CV_SCOPE_CURRENCY_INVALID",
            f"invalid source currency in full client scope: {invalid[0]}",
        )
    return currencies


def _official_cash_accounts(
    overview: Mapping[str, Any],
    scope_accounts: set[str],
    reporting_currency: str,
    liquidity: Decimal,
) -> list[Mapping[str, Any]]:
    """Validate authoritative report-date cash rows without exposing identities."""

    cash = overview.get("cash")
    rows = cash.get("accounts") if isinstance(cash, Mapping) else None
    if not isinstance(rows, list):
        raise ClientViewError(
            "CV_CASH_ROWS_MISSING",
            "official report lacks authoritative cash-account rows",
        )
    account_ids = [str(row.get("account_uuid", "")) for row in rows]
    if set(account_ids) != scope_accounts or len(account_ids) != len(set(account_ids)):
        raise ClientViewError(
            "CV_CASH_SCOPE_MISMATCH",
            "official cash-account identities differ from the client scope",
        )
    converted_total = Decimal(0)
    for row in rows:
        original_currency = str(row.get("original_currency", "")).upper()
        row_reporting_currency = str(row.get("reporting_currency", "")).upper()
        if re.fullmatch(r"[A-Z]{3}", original_currency) is None:
            raise ClientViewError(
                "CV_CASH_CURRENCY_INVALID",
                "official cash account has an invalid original currency",
            )
        if row_reporting_currency != reporting_currency:
            raise ClientViewError(
                "CV_CASH_REPORTING_CURRENCY_MISMATCH",
                "official cash account has a different reporting currency",
            )
        try:
            Decimal(str(row.get("original_value")))
            converted_total += Decimal(str(row.get("reporting_value")))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ClientViewError(
                "CV_CASH_VALUE_INVALID",
                "official cash account has an invalid value",
            ) from error
    tolerance = component_rounding_tolerance(len(rows))
    if abs(converted_total - liquidity) > tolerance:
        raise ClientViewError(
            "CV_CASH_TOTAL_MISMATCH",
            "official cash-account rows do not reconcile to liquidity",
        )
    return rows


def _activity(
    model: PortfolioModel,
    normalized: NormalizationResult,
    target_currency: str,
    cache: EcbRateCache,
    *,
    scope_accounts: set[str],
    scope_portfolios: set[str],
    scope_securities: set[str],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    rows = []
    income: dict[str, Decimal] = {}
    charges: dict[str, Decimal] = {}
    client_filters = tuple(
        group
        for group in model.client_filters
        if any(
            member.vehicle_uuid
            in (model.accounts if member.vehicle_type == "account" else model.portfolios)
            for member in group.members
        )
    )
    group_charges: dict[str, dict[str, Any]] = {
        item.uuid: {
            "name": item.name,
            "members": [
                (
                    model.accounts[member.vehicle_uuid].name
                    if member.vehicle_type == "account"
                    else model.portfolios[member.vehicle_uuid].name
                )
                for member in item.members
                if member.vehicle_uuid
                in (model.accounts if member.vehicle_type == "account" else model.portfolios)
            ],
            "categories": {},
            "instruments": {},
        }
        for item in client_filters
    }
    portfolio_transactions = {
        transaction.uuid: transaction for transaction in model.portfolio_transactions
    }

    def matching_groups(event: Any) -> list[tuple[str, Decimal]]:
        event_accounts = set(event.account_uuids)
        event_portfolios = set(event.portfolio_uuids)
        answer = []
        for group in client_filters:
            weights = []
            for member in group.members:
                if member.vehicle_type == "account" and member.vehicle_uuid in event_accounts:
                    weights.append(member.weight_raw)
                elif member.vehicle_type == "portfolio":
                    portfolio = model.portfolios.get(member.vehicle_uuid)
                    if portfolio is None:
                        continue
                    if (
                        member.vehicle_uuid in event_portfolios
                        or portfolio.reference_account_uuid in event_accounts
                    ):
                        weights.append(member.weight_raw)
            if weights:
                answer.append((group.uuid, Decimal(max(weights)) / Decimal(10000)))
        return answer

    def add_group_charge(event: Any, category: str, value: Decimal, instruments: list[str]) -> None:
        for group_uuid, ownership in matching_groups(event):
            allocated = value * ownership
            group = group_charges[group_uuid]
            categories = group["categories"]
            categories[category] = categories.get(category, Decimal(0)) + allocated
            label = " / ".join(instruments) or "Без привязки к активу"
            instrument_rows = group["instruments"]
            instrument_rows[label] = instrument_rows.get(label, Decimal(0)) + allocated
    for event in sorted(normalized.events, key=lambda item: (item.timestamp, item.event_id)):
        if not (
            set(event.account_uuids) & scope_accounts
            or set(event.portfolio_uuids) & scope_portfolios
            or set(event.security_uuids) & scope_securities
        ):
            continue
        day = event.timestamp.date()
        scoped_impacts = [
            impact for impact in event.cash_impacts if impact.account_uuid in scope_accounts
        ]
        relevant_units = list(event.units)
        if not scoped_impacts and not relevant_units:
            continue
        amount = sum(
            (
                _money(impact.amount.value, impact.currency, target_currency, day, cache)
                for impact in scoped_impacts
            ),
            Decimal(0),
        )
        # A legacy relative-reference XML can contain a BUY/SELL account leg
        # whose amount disagrees with its linked portfolio leg even though both
        # use the same currency. Portfolio Performance's official Protobuf
        # writer stores the portfolio leg and reconstructs the account leg from
        # it, so presenting the raw account amount makes XML and Binary show
        # different activity for the same official trade. Use the preserved
        # portfolio leg only for this presentation row; account valuation and
        # every official financial result continue to use their proven inputs.
        if event.canonical_type in {"BUY", "SELL"} and len(scoped_impacts) == 1:
            linked_portfolio = [
                portfolio_transactions.get(leg.leg_uuid)
                for leg in event.legs
                if leg.owner_kind == "PORTFOLIO"
            ]
            linked_portfolio = [item for item in linked_portfolio if item is not None]
            if (
                len(linked_portfolio) == 1
                and linked_portfolio[0].currency == scoped_impacts[0].currency
                and linked_portfolio[0].raw_type == event.canonical_type
            ):
                sign = Decimal(-1 if event.canonical_type == "BUY" else 1)
                amount = _money(
                    linked_portfolio[0].amount.value * sign,
                    linked_portfolio[0].currency,
                    target_currency,
                    day,
                    cache,
                )
        instruments = sorted(
            {
                model.securities[security_uuid].name
                for security_uuid in event.security_uuids
                if security_uuid in model.securities
            }
        )
        rows.append(
            {
                "date": day.isoformat(),
                "action": ACTION_LABELS.get(event.canonical_type, "Другое"),
                "instrument": " / ".join(instruments),
                "amount": decimal_text(amount),
            }
        )
        if event.canonical_type == "DIVIDENDS" and amount > 0:
            income["Дивиденды"] = income.get("Дивиденды", Decimal(0)) + amount
        elif event.canonical_type == "INTEREST" and amount > 0:
            income["Процентный доход"] = income.get("Процентный доход", Decimal(0)) + amount
        elif event.canonical_type == "INTEREST_CHARGE" and amount < 0:
            charges["Процентные расходы"] = charges.get(
                "Процентные расходы", Decimal(0)
            ) + abs(amount)
            add_group_charge(event, "Процентные расходы", abs(amount), instruments)
        elif event.canonical_type == "FEES" and amount < 0:
            charges["Комиссии"] = charges.get("Комиссии", Decimal(0)) + abs(amount)
            add_group_charge(event, "Комиссии", abs(amount), instruments)
        elif event.canonical_type == "TAXES" and amount < 0:
            charges["Налоги"] = charges.get("Налоги", Decimal(0)) + abs(amount)
            add_group_charge(event, "Налоги", abs(amount), instruments)
        for unit in relevant_units:
            converted = abs(
                _money(unit.amount.value, unit.currency, target_currency, day, cache)
            )
            if unit.type == "FEE":
                charges["Комиссии"] = charges.get("Комиссии", Decimal(0)) + converted
                add_group_charge(event, "Комиссии", converted, instruments)
            elif unit.type == "TAX":
                charges["Налоги"] = charges.get("Налоги", Decimal(0)) + converted
                add_group_charge(event, "Налоги", converted, instruments)

    def nonzero(values: Mapping[str, Decimal]) -> list[dict[str, str]]:
        return [
            {"category": category, "amount": decimal_text(amount)}
            for category, amount in sorted(values.items())
            if amount != 0
        ]

    fee_groups = []
    for group in client_filters:
        values = group_charges[group.uuid]
        categories = nonzero(values["categories"])
        instruments = [
            {"name": name, "amount": decimal_text(value)}
            for name, value in sorted(
                values["instruments"].items(), key=lambda item: (-item[1], item[0].casefold())
            )
            if value != 0
        ]
        fee_groups.append(
            {
                "id": group.uuid,
                "name": values["name"],
                "members": values["members"],
                "fees_taxes": categories,
                "instruments": instruments,
                "total": decimal_text(sum((Decimal(row["amount"]) for row in categories), Decimal(0))),
            }
        )
    return rows, {
        "income": nonzero(income),
        "fees_taxes": nonzero(charges),
        "fee_groups": fee_groups,
    }


def _fx_provenance(
    currency: str,
    source_currencies: set[str],
    periods: Mapping[str, Any],
    cache: EcbRateCache,
) -> dict[str, Any]:
    conversions = sorted(source for source in source_currencies if source != currency)
    if not conversions:
        return {
            "conversion_required": False,
            "conversions": [],
            "period_context": {},
            "observations_sha256": cache.observations_sha256,
        }

    period_context: dict[str, list[dict[str, str]]] = {}
    for period_key, period in periods.items():
        start_day = date.fromisoformat(str(period["effective_start_date"]))
        end_day = date.fromisoformat(str(period["effective_end_date"]))
        rows = []
        for source in conversions:
            start = cache.resolve(source, currency, start_day)
            end = cache.resolve(source, currency, end_day)
            rows.append(
                {
                    "source_currency": source,
                    "target_currency": currency,
                    "start_requested_date": start.requested_date.isoformat(),
                    "start_observation_date": start.observation_date.isoformat(),
                    "start_rate": decimal_text(start.rate),
                    "end_requested_date": end.requested_date.isoformat(),
                    "end_observation_date": end.observation_date.isoformat(),
                    "end_rate": decimal_text(end.rate),
                    "movement": decimal_text(end.rate / start.rate - Decimal(1)),
                    "provider": end.provider,
                }
            )
        period_context[period_key] = rows
    return {
        "conversion_required": True,
        "conversions": conversions,
        "period_context": period_context,
        "observations_sha256": cache.observations_sha256,
    }


def _rebased_period_series(
    series: list[dict[str, str]],
    start: date,
    expected_endpoint: Decimal,
) -> list[dict[str, str]]:
    anchors = [item for item in series if date.fromisoformat(item["date"]) == start]
    if len(anchors) != 1:
        raise ClientViewError(
            "CV_PERIOD_ANCHOR_MISMATCH",
            f"performance series must contain exactly one official period anchor on {start}"
        )
    selected = [item for item in series if date.fromisoformat(item["date"]) > start]
    if not selected:
        raise ClientViewError(
            "CV_PERIOD_EMPTY",
            f"performance series has no observations after {start}",
        )
    base_row = anchors[0]
    base = Decimal(base_row["cumulative_return"])
    denominator = Decimal(1) + base
    if denominator == 0:
        raise ClientViewError(
            "CV_PERIOD_BASE_NEGATIVE_100",
            "performance period cannot be rebased from -100%",
        )
    result = [
        {
            "date": item["date"],
            "market_value": item["market_value"],
            "cumulative_return": decimal_text(
                (Decimal(1) + Decimal(item["cumulative_return"])) / denominator
                - Decimal(1)
            ),
        }
        for item in selected
    ]
    calculated_endpoint = Decimal(result[-1]["cumulative_return"])
    if abs(calculated_endpoint - expected_endpoint) > RETURN_TOLERANCE:
        raise ClientViewError(
            "CV_PERIOD_ENDPOINT_MISMATCH",
            "performance period endpoint does not match the official summary"
        )
    return result


def _build_mode(
    model: PortfolioModel,
    normalized: NormalizationResult,
    valuation: ValuationResult,
    official: OfficialInputs,
    cache: EcbRateCache,
) -> dict[str, Any]:
    currency = str(official.summary["reporting_currency"]).upper()
    report_date = date.fromisoformat(str(official.summary["report_date"]))
    if report_date != valuation.valuation_date:
        raise ClientViewError(
            "CV_MODE_DATE_MISMATCH",
            "official mode date differs from the valuation date",
        )
    classifications = _classification_map(model, "assetclasses")
    sector_classifications = _classification_map(model, "industry-gics")
    geography_classifications = _geography_classification_map(model)
    overview = official.summary["portfolio_overview"]
    official_scoped = overview.get("scoped_holdings")
    scope = overview.get("scope")
    if not isinstance(official_scoped, list) or not isinstance(scope, Mapping):
        raise ClientViewError(
            "CV_HOLDINGS_MISSING",
            "official report lacks authoritative scoped holdings",
        )
    valuation_by_security = {
        item.security_uuid: item
        for item in valuation.holdings
        if item.original_market_value is not None
    }
    scoped_holdings = []
    holdings = []
    values_by_security: dict[str, Decimal] = {}
    seen: set[str] = set()
    for official_holding in official_scoped:
        security_uuid = str(official_holding["security_uuid"])
        if security_uuid in seen or security_uuid not in valuation_by_security:
            raise ClientViewError(
                "CV_HOLDING_IDENTITY_MISMATCH",
                "official scoped holding identity is missing or duplicated",
            )
        seen.add(security_uuid)
        item = valuation_by_security[security_uuid]
        if str(official_holding["currency"]).upper() != item.security_currency:
            raise ClientViewError(
                "CV_HOLDING_CURRENCY_MISMATCH",
                "official scoped holding currency differs from XML identity",
            )
        scoped_holdings.append(item)
        value = Decimal(str(official_holding["market_value"]))
        total = Decimal(str(official.summary["portfolio_market_value"]))
        values_by_security[item.security_uuid] = value
        classification = classifications.get(item.security_uuid, "Прочее")
        if classification == "Ликвидность":
            classification = "Ликвидные инструменты"
        holdings.append(
            {
                "instrument": item.security_name,
                "classification": classification,
                "instrument_currency": item.security_currency,
                "share": decimal_text(value / total if total else Decimal(0)),
                "value": decimal_text(value),
            }
        )
    holdings.sort(key=lambda item: (-Decimal(item["value"]), item["instrument"].casefold()))
    total = Decimal(str(official.summary["portfolio_market_value"]))
    liquidity = Decimal(str(official.summary["portfolio_overview"]["cash"]["market_value"]))
    holdings_total = sum((Decimal(item["value"]) for item in holdings), Decimal(0))
    difference = holdings_total + liquidity - total
    reconciliation_component_count = len(holdings) + 1
    reconciliation_tolerance = component_rounding_tolerance(
        reconciliation_component_count
    )
    if abs(difference) > reconciliation_tolerance:
        raise ClientViewError(
            "CV_PORTFOLIO_TOTAL_MISMATCH",
            f"{currency} holdings plus official liquidity do not reconcile to official total"
        )

    official_allocation = [
        {
            "name": (
                "Ликвидные инструменты"
                if str(item["name"]) == "Ликвидность"
                else str(item["name"])
            ),
            "value": decimal_text(Decimal(str(item["market_value"]))),
            "share": decimal_text(Decimal(str(item["weight"]))),
        }
        for item in official.summary["portfolio_overview"]["allocation_by_asset_class"]
        if Decimal(str(item["market_value"])) != 0
    ]
    official_allocation.append(
        {
            "name": "Свободные средства",
            "value": decimal_text(liquidity),
            "share": decimal_text(liquidity / total if total else Decimal(0)),
        }
    )
    official_allocation.sort(
        key=lambda item: (-Decimal(item["value"]), item["name"].casefold())
    )
    allocation_views = {
        "asset_class": official_allocation,
        "sector": _grouped_allocation(
            scoped_holdings,
            values_by_security,
            sector_classifications,
            liquidity,
            total,
        ),
        "geography": _geography_allocation(
            scoped_holdings,
            values_by_security,
            geography_classifications,
        ),
    }
    scope_accounts = {str(item) for item in scope.get("account_uuids", [])}
    scope_portfolios = {str(item) for item in scope.get("portfolio_uuids", [])}
    for portfolio_uuid in scope_portfolios:
        portfolio = model.portfolios.get(portfolio_uuid)
        if portfolio is not None and portfolio.reference_account_uuid:
            scope_accounts.add(portfolio.reference_account_uuid)
    official_cash_accounts = _official_cash_accounts(
        overview,
        scope_accounts,
        currency,
        liquidity,
    )
    source_currencies = {str(item.security_currency).upper() for item in scoped_holdings}
    source_currencies.update(
        str(item["original_currency"]).upper()
        for item in official_cash_accounts
        if Decimal(str(item["original_value"])) != 0
    )
    activity_rows, activity_totals = _activity(
        model,
        normalized,
        currency,
        cache,
        scope_accounts=scope_accounts,
        scope_portfolios=scope_portfolios,
        scope_securities=seen,
    )
    periods = official.summary["periods"]
    performance_series = [
        {
            "date": str(item["date"]),
            "market_value": decimal_text(Decimal(str(item["portfolio_market_value"]))),
            "cumulative_return": decimal_text(Decimal(str(item["cumulative_ttwror"]))),
        }
        for item in official.series
    ]
    all_time_endpoint = Decimal(str(periods["all_time"]["cumulative_ttwror"]))
    if not performance_series or abs(
        Decimal(performance_series[-1]["cumulative_return"]) - all_time_endpoint
    ) > RETURN_TOLERANCE:
        raise ClientViewError(
            "CV_ALL_TIME_ENDPOINT_MISMATCH",
            "all-time performance endpoint does not match the official summary"
        )
    ytd_start = date.fromisoformat(str(periods["current_year"]["effective_start_date"]))
    ytd_endpoint = Decimal(str(periods["current_year"]["cumulative_ttwror"]))
    ytd_series = _rebased_period_series(performance_series, ytd_start, ytd_endpoint)
    return {
        "currency": currency,
        "report_date": report_date.isoformat(),
        "source_scope_currencies": sorted(source_currencies),
        "summary": {
            "total_value": decimal_text(total),
            "all_time_return": decimal_text(
                Decimal(str(periods["all_time"]["cumulative_ttwror"]))
            ),
            "ytd_return": decimal_text(
                Decimal(str(periods["current_year"]["cumulative_ttwror"]))
            ),
            "all_time_profit": decimal_text(Decimal(str(periods["all_time"]["profit"]))),
            "ytd_profit": decimal_text(Decimal(str(periods["current_year"]["profit"]))),
            "liquidity": decimal_text(liquidity),
        },
        "holdings": holdings,
        "allocation": official_allocation,
        "allocation_views": allocation_views,
        "currency_exposure": _currency_exposure(
            holdings,
            official_cash_accounts,
            total,
        ),
        "activity": {
            "rows": activity_rows,
            **activity_totals,
        },
        "performance": {
            "ownership": "Portfolio Performance PerformanceIndex",
            "engine_commit": str(
                official.summary["calculation_engine"]["version_or_commit"]
            ),
            "fx_treatment": (
                "daily ECB conversion inside Portfolio Performance CurrencyConverter"
                if any(source != currency for source in source_currencies)
                else "same-currency official calculation"
            ),
            "periods": {
                "all_time": {
                    "start": str(periods["all_time"]["effective_start_date"]),
                    "end": str(periods["all_time"]["effective_end_date"]),
                },
                "ytd": {
                    "start": str(periods["current_year"]["effective_start_date"]),
                    "end": str(periods["current_year"]["effective_end_date"]),
                },
            },
            "series": performance_series,
            "period_series": {"ytd": ytd_series},
        },
        "fx": _fx_provenance(currency, source_currencies, periods, cache),
        "reconciliation": {
            "source": "Portfolio Performance official report-date snapshot",
            "holdings_total": decimal_text(holdings_total),
            "liquidity": decimal_text(liquidity),
            "displayed_total": decimal_text(total),
            "difference": decimal_text(difference),
            "component_count": reconciliation_component_count,
            "tolerance": decimal_text(reconciliation_tolerance),
            "status": "PASS",
        },
    }


def build_client_views(
    model: PortfolioModel,
    normalized: NormalizationResult,
    valuation: ValuationResult,
    official_modes: Mapping[str, OfficialInputs],
    cache: EcbRateCache,
) -> dict[str, Any]:
    if not official_modes:
        raise ClientViewError("CV_MODES_EMPTY", "official client modes cannot be empty")
    normalized_modes: dict[str, OfficialInputs] = {}
    for key, official in official_modes.items():
        currency = str(key).strip().upper()
        if re.fullmatch(r"[A-Z]{3}", currency) is None:
            raise ClientViewError(
                "CV_REPORTING_CURRENCY_INVALID",
                f"invalid reporting currency code: {key}",
            )
        if currency in normalized_modes:
            raise ClientViewError(
                "CV_MODE_DUPLICATE",
                f"duplicate reporting currency mode: {currency}",
            )
        if str(official.summary.get("reporting_currency", "")).upper() != currency:
            raise ClientViewError(
                "CV_MODE_KEY_MISMATCH",
                f"official reporting currency does not match mode key: {currency}"
            )
        normalized_modes[currency] = official
    preferred_default = str(model.base_currency).upper()
    default_currency = (
        preferred_default
        if preferred_default in normalized_modes
        else sorted(normalized_modes)[0]
    )
    available_currencies = [
        default_currency,
        *(currency for currency in sorted(normalized_modes) if currency != default_currency),
    ]
    modes = {
        currency: _build_mode(
            model,
            normalized,
            valuation,
            normalized_modes[currency],
            cache,
        )
        for currency in available_currencies
    }
    return {
        "schema_version": CLIENT_VIEWS_SCHEMA_VERSION,
        "default_currency": default_currency,
        "available_currencies": available_currencies,
        "financial_arithmetic": "build-time-only",
        "modes": modes,
    }


def build_grouped_client_views(
    model: PortfolioModel,
    normalized: NormalizationResult,
    valuation: ValuationResult,
    official_group_modes: Mapping[str, Mapping[str, OfficialInputs]],
    cache: EcbRateCache,
) -> dict[str, Any]:
    """Build a presentation matrix without performing browser-side arithmetic."""
    scopes = {scope.id: scope for scope in model.group_scopes}
    if set(official_group_modes) != set(scopes):
        raise ClientViewError(
            "CV_SCOPE_EVIDENCE_MISSING",
            "official group matrix does not match the XML Grouped Accounts inventory",
        )
    grouped: dict[str, Any] = {}
    for scope in model.group_scopes:
        payload = build_client_views(
            model,
            normalized,
            valuation,
            official_group_modes[scope.id],
            cache,
        )
        scope_metadata = {
            "id": scope.id,
            "name": scope.name,
            "filter_uuid": scope.filter_uuid,
            "account_ids": list(scope.account_uuids),
            "portfolio_ids": list(scope.portfolio_uuids),
        }
        for mode in payload["modes"].values():
            mode["group_scope"] = scope_metadata
        grouped[scope.id] = {
            "id": scope.id,
            "name": scope.name,
            "filter_uuid": scope.filter_uuid,
            "account_ids": list(scope.account_uuids),
            "portfolio_ids": list(scope.portfolio_uuids),
            "members": [
                {
                    "vehicle_type": member.vehicle_type,
                    "vehicle_uuid": member.vehicle_uuid,
                    "weight_raw": member.weight_raw,
                }
                for member in scope.members
            ],
            "default_currency": payload["default_currency"],
            "available_currencies": payload["available_currencies"],
            "modes": payload["modes"],
        }
    full = grouped[model.group_scopes[0].id]
    return {
        "schema_version": CLIENT_VIEWS_SCHEMA_VERSION,
        "default_currency": full["default_currency"],
        "available_currencies": full["available_currencies"],
        "financial_arithmetic": "build-time-only",
        "modes": full["modes"],
        "group_matrix": {
            "schema_version": "1.0",
            "default_group": model.group_scopes[0].id,
            "available_groups": [
                {"id": scope.id, "name": scope.name} for scope in model.group_scopes
            ],
            "groups": grouped,
        },
    }


def normalized_client_json(payload: Mapping[str, Any], *, indent: int | None = None) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":") if indent is None else None,
        indent=indent,
    )


def client_payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(normalized_client_json(payload).encode("utf-8")).hexdigest()
