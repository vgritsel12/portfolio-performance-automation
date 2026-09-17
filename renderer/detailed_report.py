#!/usr/bin/env python3
"""Build a deterministic, presentation-only dashboard report from canonical state."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence

from renderer.currency_views import (
    ConversionStatus,
    CurrencyViewMode,
    ReportViews,
    build_currency_views,
)
from renderer.portfolio_valuation import ValuationResult, build_valuation
from renderer.portfolio_xml_model import (
    CanonicalEvent,
    NormalizationResult,
    PortfolioModel,
    Taxonomy,
    TaxonomyNode,
    normalize_transactions,
)


SCHEMA_VERSION = "1.0"
SECTION_KEYS = (
    "summary",
    "performance",
    "holdings",
    "currencies",
    "allocation",
    "transactions",
    "income",
    "fees_taxes",
    "accounts",
    "securities",
)
DECIMAL_PATTERN = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
UUID_PATTERN = re.compile(r"^[0-9a-fA-F-]{8,64}$")


class DetailedReportError(ValueError):
    """A detailed report input or schema contract failed."""


@dataclass(frozen=True)
class OfficialInputs:
    summary: Mapping[str, Any]
    series: tuple[Mapping[str, str], ...]
    summary_path: str
    series_path: str


def decimal_text(value: Decimal | int | str) -> str:
    """Return a finite, non-exponent decimal string for the JSON contract."""
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise DetailedReportError(f"invalid decimal value: {value!r}") from error
    if not number.is_finite():
        raise DetailedReportError(f"non-finite decimal value: {value!r}")
    result = format(number, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    if result in {"", "-0"}:
        return "0"
    return result


def safe_id(kind: str, identity: str) -> str:
    normalized = identity.strip()
    if UUID_PATTERN.fullmatch(normalized):
        token = normalized.lower()
    else:
        token = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{kind}-{token}"


def _json_compatible(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_compatible(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise DetailedReportError(f"value is not JSON-compatible: {type(value).__name__}")


def normalized_json(report: Mapping[str, Any], *, indent: int | None = None) -> str:
    return json.dumps(
        _json_compatible(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":") if indent is None else None,
        indent=indent,
    )


def script_safe_json(report: Mapping[str, Any]) -> str:
    """Serialize for an inline application/json script without HTML breakout."""
    return (
        normalized_json(report)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def load_official_inputs(summary_path: Path, series_path: Path) -> OfficialInputs:
    try:
        summary = json.loads(
            summary_path.read_text(encoding="utf-8"),
            parse_float=Decimal,
            parse_int=Decimal,
        )
    except (OSError, json.JSONDecodeError, InvalidOperation) as error:
        raise DetailedReportError(f"cannot load official Summary report: {error}") from error
    if not isinstance(summary, dict):
        raise DetailedReportError("official Summary report root must be an object")
    required = {
        "source_file",
        "reporting_currency",
        "report_date",
        "periods",
        "portfolio_market_value",
        "portfolio_overview",
        "calculation_engine",
    }
    missing = sorted(required - set(summary))
    if missing:
        raise DetailedReportError(f"official Summary report missing fields: {', '.join(missing)}")
    try:
        with series_path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is None:
                raise DetailedReportError("official performance series has no header")
            expected = {
                "date",
                "portfolio_market_value",
                "period_profit",
                "cumulative_ttwror",
                "daily_ttwror",
            }
            if set(reader.fieldnames) != expected:
                raise DetailedReportError("official performance series header differs from v2 contract")
            rows = []
            for row_number, row in enumerate(reader, start=2):
                parsed = {"date": str(row["date"])}
                for field in sorted(expected - {"date"}):
                    try:
                        parsed[field] = decimal_text(Decimal(str(row[field])))
                    except (InvalidOperation, DetailedReportError) as error:
                        raise DetailedReportError(
                            f"official performance series row {row_number} has invalid {field}"
                        ) from error
                rows.append(parsed)
    except OSError as error:
        raise DetailedReportError(f"cannot load official performance series: {error}") from error
    return OfficialInputs(summary, tuple(rows), summary_path.name, series_path.name)


def _amount_rows(event: CanonicalEvent) -> list[dict[str, Any]]:
    return [
        {
            "account_id": safe_id("account", impact.account_uuid),
            "account_uuid": impact.account_uuid,
            "currency": impact.currency,
            "amount": decimal_text(impact.amount.value),
            "source_leg_uuid": impact.source_leg_uuid,
        }
        for impact in event.cash_impacts
    ]


def _transaction_rows(events: Iterable[CanonicalEvent]) -> list[dict[str, Any]]:
    rows = []
    for event in sorted(events, key=lambda item: (item.timestamp, item.event_id)):
        rows.append(
            {
                "id": safe_id("event", event.event_id),
                "event_id": event.event_id,
                "date": event.timestamp.date().isoformat(),
                "timestamp": event.timestamp.isoformat(),
                "type": event.canonical_type,
                "raw_types": list(event.raw_types),
                "category": event.display_category.value,
                "label": event.display_label,
                "security_ids": [safe_id("security", item) for item in event.security_uuids],
                "account_ids": [safe_id("account", item) for item in event.account_uuids],
                "portfolio_ids": [safe_id("portfolio", item) for item in event.portfolio_uuids],
                "cash_impacts": _amount_rows(event),
                "quantity_impacts": [
                    {
                        "portfolio_id": safe_id("portfolio", impact.portfolio_uuid),
                        "security_id": safe_id("security", impact.security_uuid),
                        "shares": decimal_text(impact.shares.value),
                        "source_leg_uuid": impact.source_leg_uuid,
                    }
                    for impact in event.quantity_impacts
                ],
                "units": [
                    {
                        "type": unit.type,
                        "currency": unit.currency,
                        "amount": decimal_text(unit.amount.value),
                        "forex_currency": unit.forex_currency,
                        "forex_amount": (
                            decimal_text(unit.forex_amount.value) if unit.forex_amount else None
                        ),
                        "exchange_rate": (
                            decimal_text(unit.exchange_rate) if unit.exchange_rate is not None else None
                        ),
                        "source_path": unit.source.path,
                    }
                    for unit in event.units
                ],
                "notes": list(event.notes),
                "display_hints": list(event.display_hints),
                "transfer_status": event.transfer_status.value,
                "provenance": {
                    "legs": [
                        {
                            "uuid": leg.leg_uuid,
                            "owner_kind": leg.owner_kind,
                            "owner_id": safe_id(leg.owner_kind.lower(), leg.owner_uuid),
                            "raw_type": leg.raw_type,
                            "source_path": leg.source.path,
                        }
                        for leg in event.legs
                    ]
                },
            }
        )
    return rows


def _add_bucket(target: dict[str, dict[str, Decimal]], currency: str, key: str, amount: Decimal) -> None:
    target.setdefault(currency, {}).setdefault(key, Decimal(0))
    target[currency][key] += amount


def _income_and_charges(
    events: Iterable[CanonicalEvent], valuation: ValuationResult
) -> tuple[dict[str, Any], dict[str, Any]]:
    technical_accounts = {
        account.account_uuid for account in valuation.accounts if account.technical.is_technical
    }
    income_totals: dict[str, dict[str, Decimal]] = {}
    charge_totals: dict[str, dict[str, Decimal]] = {}
    income_rows = []
    charge_rows = []
    for event in sorted(events, key=lambda item: (item.timestamp, item.event_id)):
        for impact in event.cash_impacts:
            income_kind = None
            charge_kind = None
            if event.canonical_type == "DIVIDENDS":
                income_kind = "dividends"
            elif event.canonical_type == "INTEREST":
                income_kind = (
                    "accrued_aci"
                    if impact.account_uuid in technical_accounts
                    else "realized_interest"
                )
            elif event.canonical_type == "INTEREST_CHARGE":
                charge_kind = "interest_charges"
            elif (
                event.canonical_type == "REMOVAL"
                and "POSSIBLE_OTHER_CHARGE_FROM_NOTE" in event.display_hints
            ):
                charge_kind = "other_charges"
            if income_kind is not None:
                _add_bucket(income_totals, impact.currency, income_kind, impact.amount.value)
                income_rows.append(
                    {
                        "event_id": event.event_id,
                        "date": event.timestamp.date().isoformat(),
                        "kind": income_kind,
                        "currency": impact.currency,
                        "amount": decimal_text(impact.amount.value),
                        "account_id": safe_id("account", impact.account_uuid),
                    }
                )
            if charge_kind is not None:
                value = abs(impact.amount.value)
                _add_bucket(charge_totals, impact.currency, charge_kind, value)
                charge_rows.append(
                    {
                        "event_id": event.event_id,
                        "date": event.timestamp.date().isoformat(),
                        "kind": charge_kind,
                        "currency": impact.currency,
                        "amount": decimal_text(value),
                        "classification_status": (
                            "UNCERTAIN_NOTE" if charge_kind == "other_charges" else "PP_TYPE"
                        ),
                    }
                )
        for unit in event.units:
            if unit.type not in {"FEE", "TAX"}:
                continue
            key = "fees" if unit.type == "FEE" else "taxes"
            _add_bucket(charge_totals, unit.currency, key, unit.amount.value)
            charge_rows.append(
                {
                    "event_id": event.event_id,
                    "date": event.timestamp.date().isoformat(),
                    "kind": key,
                    "currency": unit.currency,
                    "amount": decimal_text(unit.amount.value),
                    "classification_status": "PP_UNIT",
                    "source_path": unit.source.path,
                }
            )
    income_keys = ("dividends", "realized_interest", "accrued_aci")
    charge_keys = ("fees", "taxes", "interest_charges", "other_charges")
    currencies = sorted(set(income_totals) | set(charge_totals))
    income_by_currency = {
        code: {key: decimal_text(income_totals.get(code, {}).get(key, Decimal(0))) for key in income_keys}
        for code in currencies
    }
    charge_by_currency = {
        code: {key: decimal_text(charge_totals.get(code, {}).get(key, Decimal(0))) for key in charge_keys}
        for code in currencies
    }
    return (
        {"by_currency": income_by_currency, "rows": income_rows},
        {"by_currency": charge_by_currency, "rows": charge_rows},
    )


def _vehicle_values(
    valuation: ValuationResult,
) -> dict[tuple[str, str], tuple[Decimal, str, str]]:
    values: dict[tuple[str, str], tuple[Decimal, str, str]] = {}
    holdings: dict[str, tuple[Decimal, str]] = {}
    for item in valuation.holdings:
        if item.original_market_value is None:
            continue
        current, currency = holdings.get(item.security_uuid, (Decimal(0), item.security_currency))
        holdings[item.security_uuid] = (current + item.original_market_value, currency)
    for uuid, (value, currency) in holdings.items():
        values[("security", uuid)] = (value, currency, safe_id("security", uuid))
    for item in valuation.accounts:
        values[("account", item.account_uuid)] = (
            item.balance.value,
            item.currency,
            safe_id("account", item.account_uuid),
        )
    return values


def _taxonomy_report(taxonomy: Taxonomy, valuation: ValuationResult) -> dict[str, Any]:
    vehicle_values = _vehicle_values(valuation)
    residual = {key: 10000 for key in vehicle_values}

    def walk(
        node: TaxonomyNode,
        path_ids: tuple[str, ...],
        path_names: tuple[str, ...],
        depth: int,
    ) -> tuple[dict[str, Any], dict[str, Decimal]]:
        current_ids = path_ids + (node.id,)
        current_names = path_names + (node.name,)
        direct: dict[str, Decimal] = {}
        assignments = []
        for assignment in node.assignments:
            key = (assignment.investment_vehicle_type, assignment.investment_vehicle_uuid)
            requested = max(0, assignment.weight.raw)
            remaining = residual.get(key, 0)
            effective = min(requested, remaining)
            if key in residual:
                residual[key] -= effective
            value_info = vehicle_values.get(key)
            allocation = None
            currency = None
            vehicle_id = safe_id(assignment.investment_vehicle_type, assignment.investment_vehicle_uuid)
            if value_info is not None:
                vehicle_value, currency, vehicle_id = value_info
                allocation = vehicle_value * Decimal(effective) / Decimal(10000)
                direct[currency] = direct.get(currency, Decimal(0)) + allocation
            assignments.append(
                {
                    "vehicle_type": assignment.investment_vehicle_type,
                    "vehicle_uuid": assignment.investment_vehicle_uuid,
                    "vehicle_id": vehicle_id,
                    "requested_weight_raw": assignment.weight.raw,
                    "effective_weight_raw": effective,
                    "effective_weight": decimal_text(Decimal(effective) / Decimal(100)),
                    "currency": currency,
                    "allocated_value": decimal_text(allocation) if allocation is not None else None,
                    "value_status": "CURRENT" if value_info is not None else "NOT_CURRENT",
                    "source_path": assignment.source.path,
                }
            )
        children = []
        cumulative = dict(direct)
        for child in node.children:
            child_report, child_values = walk(child, current_ids, current_names, depth + 1)
            children.append(child_report)
            for code, amount in child_values.items():
                cumulative[code] = cumulative.get(code, Decimal(0)) + amount
        report = {
            "id": safe_id("taxonomy-node", node.id),
            "node_id": node.id,
            "name": node.name,
            "color": node.color,
            "rank": node.rank,
            "weight_raw": node.weight.raw,
            "weight": decimal_text(node.weight.value),
            "data": {key: value for key, value in node.data},
            "depth": depth,
            "path_ids": list(current_ids),
            "path_names": list(current_names),
            "source_path": node.source.path,
            "assignments": assignments,
            "direct_allocation_by_currency": {
                code: decimal_text(value) for code, value in sorted(direct.items())
            },
            "allocation_by_currency": {
                code: decimal_text(value) for code, value in sorted(cumulative.items())
            },
            "children": children,
        }
        return report, cumulative

    root, allocated = walk(taxonomy.root, (), (), 0)
    unclassified_rows = []
    unclassified_by_currency: dict[str, Decimal] = {}
    for key, remaining in sorted(residual.items()):
        if remaining <= 0:
            continue
        value, currency, vehicle_id = vehicle_values[key]
        allocation = value * Decimal(remaining) / Decimal(10000)
        unclassified_by_currency[currency] = (
            unclassified_by_currency.get(currency, Decimal(0)) + allocation
        )
        unclassified_rows.append(
            {
                "vehicle_type": key[0],
                "vehicle_uuid": key[1],
                "vehicle_id": vehicle_id,
                "residual_weight_raw": remaining,
                "residual_weight": decimal_text(Decimal(remaining) / Decimal(100)),
                "currency": currency,
                "allocated_value": decimal_text(allocation),
            }
        )
    return {
        "id": safe_id("taxonomy", taxonomy.id),
        "taxonomy_id": taxonomy.id,
        "name": taxonomy.name,
        "dimensions": list(taxonomy.dimensions),
        "source_path": taxonomy.source.path,
        "root": root,
        "classified_by_currency": {
            code: decimal_text(value) for code, value in sorted(allocated.items())
        },
        "unclassified_by_currency": {
            code: decimal_text(value) for code, value in sorted(unclassified_by_currency.items())
        },
        "unclassified": unclassified_rows,
    }


def _holdings_section(valuation: ValuationResult) -> dict[str, Any]:
    rows = []
    for item in valuation.holdings:
        rows.append(
            {
                "id": safe_id("holding", f"{item.portfolio_uuid}:{item.security_uuid}"),
                "security_id": safe_id("security", item.security_uuid),
                "security_uuid": item.security_uuid,
                "name": item.security_name,
                "isin": item.isin,
                "portfolio_id": safe_id("portfolio", item.portfolio_uuid),
                "portfolio_uuid": item.portfolio_uuid,
                "portfolio_name": item.portfolio_name,
                "quantity": decimal_text(item.quantity.value),
                "price": decimal_text(item.price.value) if item.price else None,
                "price_date": item.price_date.isoformat() if item.price_date else None,
                "price_status": item.price_status.value,
                "currency": item.security_currency,
                "market_value": (
                    decimal_text(item.original_market_value)
                    if item.original_market_value is not None
                    else None
                ),
                "cost_basis": None,
                "unrealized_gain": None,
                "availability": {
                    "cost_basis": "UNAVAILABLE_NOT_VALIDATED",
                    "unrealized_gain": "UNAVAILABLE_NOT_VALIDATED",
                },
                "reconciliation": item.reconciliation.status.value,
            }
        )
    return {"rows": rows, "closed_position_keys": [list(item) for item in valuation.closed_positions]}


def _account_rows(model: PortfolioModel, valuation: ValuationResult) -> list[dict[str, Any]]:
    return [
        {
            "id": safe_id("account", item.account_uuid),
            "uuid": item.account_uuid,
            "name": item.name,
            "currency": item.currency,
            "balance": decimal_text(item.balance.value),
            "retired": item.retired,
            "transaction_count": item.transaction_count,
            "technical": {
                "is_technical": item.technical.is_technical,
                "kind": item.technical.kind,
                "heuristic": item.technical.heuristic_used,
                "provenance": list(item.technical.provenance),
            },
            "reconciliation": item.reconciliation.status.value,
            "source_path": model.accounts[item.account_uuid].source.path,
        }
        for item in valuation.accounts
    ]


def _security_rows(model: PortfolioModel, valuation: ValuationResult) -> list[dict[str, Any]]:
    current = {item.security_uuid for item in valuation.holdings}
    closed = {security for _, security in valuation.closed_positions}
    return [
        {
            "id": safe_id("security", item.uuid),
            "uuid": item.uuid,
            "name": item.name,
            "isin": item.isin,
            "currency": item.currency,
            "retired": item.retired,
            "position_status": "CURRENT" if item.uuid in current else "CLOSED" if item.uuid in closed else "UNHELD",
            "price_points": len(item.prices),
            "last_price_date": item.prices[-1].day.isoformat() if item.prices else None,
            "source_path": item.source.path,
        }
        for item in sorted(model.securities.values(), key=lambda value: value.uuid)
    ]


def _currency_section(views: ReportViews) -> dict[str, Any]:
    per_currency = {}
    for code, view in sorted(views.per_currency.items()):
        per_currency[code] = {
            "holding_count": len(view.holdings),
            "account_count": len(view.accounts),
            "event_count": len(view.events),
            "market_value": decimal_text(view.original_market_value),
            "cash_balance": decimal_text(view.original_cash_balance),
            "total_value": decimal_text(view.original_total_value),
            "performance_status": view.performance_status.value,
            "performance_reason": view.performance_reason,
            "external_flow_status": view.external_flows.status.value,
            "uncertain_event_ids": list(view.external_flows.uncertain_event_ids),
        }
    consolidated = None
    if views.consolidated is not None:
        total = views.consolidated.total_value
        consolidated = {
            "reporting_currency": views.consolidated.reporting_currency,
            "status": total.status.value,
            "complete_value": decimal_text(total.complete_value) if total.complete_value is not None else None,
            "available_value": decimal_text(total.available_value),
            "missing_labels": list(total.missing_labels),
            "components": [
                {
                    "label": item.label,
                    "original_value": decimal_text(item.original_value),
                    "original_currency": item.original_currency,
                    "conversion_date": item.conversion_date.isoformat(),
                    "converted_value": decimal_text(item.converted_value) if item.converted_value is not None else None,
                    "status": item.status.value,
                    "fx_rate": decimal_text(item.fx_rate) if item.fx_rate is not None else None,
                    "fx_rate_date": item.fx_rate_date.isoformat() if item.fx_rate_date else None,
                    "fx_source": item.fx_source,
                    "reason": item.reason,
                }
                for item in total.components
            ],
            "performance_status": views.consolidated.performance_status.value,
            "performance_reason": views.consolidated.performance_reason,
        }
    return {
        "mode": views.mode.value,
        "reporting_currency": views.reporting_currency.code,
        "per_currency": per_currency,
        "consolidated": consolidated,
        "comparative_returns": None,
    }


def _official_scope(model: PortfolioModel, summary: Mapping[str, Any]) -> dict[str, Any]:
    reporting = str(summary["reporting_currency"]).upper()
    names = [item.name for item in model.dashboards if reporting in item.name.upper()]
    return {
        "source": "Portfolio Performance saved dashboards",
        "reporting_currency": reporting,
        "saved_dashboard_names": names,
        "resolution_status": "RESOLVED" if names else "UNAVAILABLE",
        "portfolio_scope": "all portfolios/accounts in the saved client filter",
    }


def build_detailed_report(
    model: PortfolioModel,
    official: OfficialInputs,
    normalized: NormalizationResult | None = None,
    valuation: ValuationResult | None = None,
    views: ReportViews | None = None,
    *,
    source_sha256: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise DetailedReportError("source_sha256 must be a lowercase SHA-256 digest")
    normalized = normalized or normalize_transactions(model)
    valuation = valuation or build_valuation(model, normalized)
    views = views or build_currency_views(
        model,
        valuation,
        normalized,
        mode=CurrencyViewMode.BOTH,
        reporting_currency=str(official.summary["reporting_currency"]),
    )
    official_scope = _official_scope(model, official.summary)
    income, charges = _income_and_charges(normalized.events, valuation)
    official_summary = {
        "source_file": official.summary["source_file"],
        "reporting_currency": official.summary["reporting_currency"],
        "report_date": official.summary["report_date"],
        "portfolio_market_value": decimal_text(official.summary["portfolio_market_value"]),
        "portfolio_overview": official.summary["portfolio_overview"],
        "periods": official.summary["periods"],
        "calculation_engine": official.summary["calculation_engine"],
        "scope": official_scope,
    }
    unavailable = [
        "cost basis and unrealized gain: no independently validated implementation",
        "per-currency return series: no independently validated scoped series",
    ]
    if (
        views.consolidated is not None
        and views.consolidated.total_value.status
        in {ConversionStatus.MISSING, ConversionStatus.PARTIAL}
    ):
        unavailable.append(
            "consolidated XML-derived cross-currency total: dated FX evidence is incomplete"
        )
    report = {
        "schema_version": SCHEMA_VERSION,
        "metadata": {
            "source_file": Path(model.source_file).name,
            "source_sha256": source_sha256,
            "portfolio_performance_xml_version": model.version,
            "base_currency": model.base_currency,
            "valuation_date": valuation.valuation_date.isoformat(),
            "official_summary_path": official.summary_path,
            "official_series_path": official.series_path,
            "official_scope": official_scope,
            "decimal_contract": "finite base-10 strings; no exponent",
        },
        "sections": {
            "summary": {
                "official": official_summary,
                "canonical_event_count": len(normalized.events),
                "current_holding_count": len(valuation.holdings),
                "account_count": len(valuation.accounts),
                "security_count": len(model.securities),
            },
            "performance": {
                "source": "Portfolio Performance official Summary engine",
                "scope": official_scope,
                "periods": official.summary["periods"],
                "series": list(official.series),
                "series_unit": {"cumulative_ttwror": "DECIMAL_RETURN", "portfolio_market_value": str(official.summary["reporting_currency"])},
            },
            "holdings": _holdings_section(valuation),
            "currencies": _currency_section(views),
            "allocation": {
                "taxonomies": [_taxonomy_report(item, valuation) for item in model.taxonomies]
            },
            "transactions": {"rows": _transaction_rows(normalized.events)},
            "income": income,
            "fees_taxes": charges,
            "accounts": {"rows": _account_rows(model, valuation)},
            "securities": {"rows": _security_rows(model, valuation)},
        },
        "diagnostics": {
            "schema_validation": "PASS",
            "reference_resolution": model.reference_stats.__dict__,
            "valuation_warnings": list(valuation.warnings),
            "unavailable": unavailable,
            "source_xml_embedded": False,
        },
    }
    errors = validate_detailed_report(report)
    if errors:
        raise DetailedReportError("detailed report schema validation failed: " + "; ".join(errors))
    return report


def validate_detailed_report(report: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate strict structural invariants represented by the JSON Schema artifact."""
    errors = []
    expected_root = {"schema_version", "metadata", "sections", "diagnostics"}
    if set(report) != expected_root:
        errors.append("root keys differ from strict schema")
    if report.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version must be 1.0")
    sections = report.get("sections")
    if not isinstance(sections, Mapping):
        errors.append("sections must be an object")
        return tuple(errors)
    if set(sections) != set(SECTION_KEYS):
        errors.append("sections must contain exactly the ten section keys")
    array_paths = {
        "holdings": "rows",
        "transactions": "rows",
        "accounts": "rows",
        "securities": "rows",
        "allocation": "taxonomies",
    }
    for section, field in array_paths.items():
        value = sections.get(section)
        if not isinstance(value, Mapping) or not isinstance(value.get(field), list):
            errors.append(f"sections.{section}.{field} must be an array")
    transaction_rows = sections.get("transactions", {}).get("rows", [])
    event_ids = [row.get("event_id") for row in transaction_rows if isinstance(row, Mapping)]
    if len(event_ids) != len(set(event_ids)):
        errors.append("transaction event IDs must be unique")
    for section in ("holdings", "accounts", "securities"):
        rows = sections.get(section, {}).get("rows", [])
        ids = [row.get("id") for row in rows if isinstance(row, Mapping)]
        if len(ids) != len(set(ids)) or any(not isinstance(item, str) for item in ids):
            errors.append(f"sections.{section} IDs must be unique strings")
    if report.get("diagnostics", {}).get("source_xml_embedded") is not False:
        errors.append("source XML must not be embedded")
    def check_decimals(value: Any, path: str = "$") -> None:
        if isinstance(value, str) and any(token in path for token in ("amount", "value", "weight", "shares", "price", "balance", "ttwror", "irr", "profit")):
            if value and value not in {"USD", "EUR", "DECIMAL_RETURN"} and not DECIMAL_PATTERN.fullmatch(value):
                # Text-bearing labels such as availability reasons are not decimal fields.
                leaf = path.rsplit(".", 1)[-1]
                if leaf in {"amount", "allocated_value", "market_value", "total_value", "cash_balance", "balance", "quantity", "price", "weight", "effective_weight", "residual_weight", "shares", "fx_rate", "converted_value", "original_value", "complete_value", "available_value"}:
                    errors.append(f"{path} is not a canonical decimal string")
        elif isinstance(value, Mapping):
            for key, item in value.items():
                check_decimals(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                check_decimals(item, f"{path}[{index}]")
    check_decimals(report)
    return tuple(errors)


def audit_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    sections = report["sections"]
    def node_count(node: Mapping[str, Any]) -> int:
        return 1 + sum(node_count(child) for child in node["children"])
    taxonomy_rows = sections["allocation"]["taxonomies"]
    return {
        "schema_version": report["schema_version"],
        "schema_validation": "PASS" if not validate_detailed_report(report) else "FAIL",
        "sections": list(sections),
        "counts": {
            "holdings": len(sections["holdings"]["rows"]),
            "accounts": len(sections["accounts"]["rows"]),
            "securities": len(sections["securities"]["rows"]),
            "transactions": len(sections["transactions"]["rows"]),
            "performance_points": len(sections["performance"]["series"]),
            "taxonomies": len(taxonomy_rows),
            "taxonomy_nodes": sum(node_count(item["root"]) for item in taxonomy_rows),
        },
        "currencies": sorted(sections["currencies"]["per_currency"]),
        "consolidated_status": sections["currencies"]["consolidated"]["status"],
        "income_by_currency": sections["income"]["by_currency"],
        "charges_by_currency": sections["fees_taxes"]["by_currency"],
        "normalized_sha256": hashlib.sha256(normalized_json(report).encode("utf-8")).hexdigest(),
        "source_xml_embedded": report["diagnostics"]["source_xml_embedded"],
    }


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit the canonical detailed dashboard report")
    parser.add_argument("--audit", required=True, type=Path, metavar="XML")
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--series", required=True, type=Path)
    parser.add_argument("--write", type=Path, help="optional detailed JSON output")
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = _parse_args(argv)
    try:
        model = PortfolioModel.from_path(args.audit)
        official = load_official_inputs(args.summary, args.series)
        report = build_detailed_report(
            model,
            official,
            source_sha256=hashlib.sha256(args.audit.read_bytes()).hexdigest(),
        )
        if args.write:
            args.write.write_text(normalized_json(report, indent=2) + "\n", encoding="utf-8")
    except (OSError, DetailedReportError, ValueError) as error:
        sys.stderr.write(f"Detailed report audit failed: {error}\n")
        return 2
    sys.stdout.write(json.dumps(audit_summary(report), ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
