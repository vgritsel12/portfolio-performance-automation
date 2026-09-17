#!/usr/bin/env python3
"""Original-currency valuation and reconciliation for canonical PP models."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
import json
from pathlib import Path
import sys
from typing import Iterable

from renderer.portfolio_xml_model import (
    Account,
    NormalizationResult,
    PortfolioModel,
    PricePoint,
    RawTransaction,
    ScaledValue,
    Security,
    cash_sign_for_type,
    money_value,
    normalize_transactions,
    quantity_sign_for_type,
    share_value,
)


CENT = Decimal("0.01")


class PriceStatus(str, Enum):
    EXACT = "EXACT"
    CARRIED_FORWARD = "CARRIED_FORWARD"
    STALE = "STALE"
    MISSING = "MISSING"


class ReconciliationStatus(str, Enum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"


@dataclass(frozen=True)
class PriceSelection:
    requested_date: date
    point: PricePoint | None
    status: PriceStatus
    age_days: int | None
    reason: str | None


@dataclass(frozen=True)
class TechnicalAccountClassification:
    is_technical: bool
    kind: str | None
    provenance: tuple[str, ...]
    heuristic_used: bool


@dataclass(frozen=True)
class Reconciliation:
    status: ReconciliationStatus
    expected_raw: int
    actual_raw: int
    delta_raw: int
    reason: str | None = None


@dataclass(frozen=True)
class AccountValuation:
    account_uuid: str
    name: str
    currency: str
    balance: ScaledValue
    retired: bool
    transaction_count: int
    technical: TechnicalAccountClassification
    reconciliation: Reconciliation


@dataclass(frozen=True)
class HoldingValuation:
    security_uuid: str
    security_name: str
    isin: str | None
    portfolio_uuid: str
    portfolio_name: str
    quantity: ScaledValue
    price: ScaledValue | None
    price_date: date | None
    price_status: PriceStatus
    price_age_days: int | None
    security_currency: str
    original_market_value: Decimal | None
    reconciliation: Reconciliation
    cost_basis: None = None
    unrealized_gain: None = None


@dataclass(frozen=True)
class ValuationResult:
    valuation_date: date
    accounts: tuple[AccountValuation, ...]
    holdings: tuple[HoldingValuation, ...]
    closed_positions: tuple[tuple[str, str], ...]
    price_status_counts: dict[str, int]
    warnings: tuple[str, ...]

    def account_by_uuid(self) -> dict[str, AccountValuation]:
        return {item.account_uuid: item for item in self.accounts}

    def holding_by_key(self) -> dict[tuple[str, str], HoldingValuation]:
        return {(item.portfolio_uuid, item.security_uuid): item for item in self.holdings}

    def audit_summary(self) -> dict[str, object]:
        return {
            "valuation_date": self.valuation_date.isoformat(),
            "accounts": len(self.accounts),
            "technical_accounts": sum(item.technical.is_technical for item in self.accounts),
            "account_currencies": sorted({item.currency for item in self.accounts}),
            "account_reconciliation": _count(item.reconciliation.status.value for item in self.accounts),
            "current_holdings": len(self.holdings),
            "closed_positions": len(self.closed_positions),
            "holding_currencies": sorted({item.security_currency for item in self.holdings}),
            "holding_reconciliation": _count(item.reconciliation.status.value for item in self.holdings),
            "price_status": dict(sorted(self.price_status_counts.items())),
            "warnings": list(self.warnings),
        }


def derive_valuation_date(model: PortfolioModel) -> date:
    dates = [transaction.timestamp.date() for transaction in model.raw_transactions]
    dates.extend(point.day for security in model.securities.values() for point in security.prices)
    if not dates:
        raise ValueError("valuation date is unavailable: XML has no transaction or price dates")
    return max(dates)


def select_price(
    security: Security,
    requested_date: date,
    *,
    stale_after_days: int = 31,
) -> PriceSelection:
    candidates = [point for point in security.prices if point.day <= requested_date]
    if not candidates:
        future = min((point.day for point in security.prices if point.day > requested_date), default=None)
        reason = (
            f"only future prices exist; first is {future.isoformat()}"
            if future is not None
            else "security has no prices"
        )
        return PriceSelection(requested_date, None, PriceStatus.MISSING, None, reason)
    point = max(candidates, key=lambda item: item.day)
    age = (requested_date - point.day).days
    status = (
        PriceStatus.EXACT
        if age == 0
        else PriceStatus.CARRIED_FORWARD
        if age <= stale_after_days
        else PriceStatus.STALE
    )
    reason = None if status != PriceStatus.STALE else f"price is {age} days old"
    return PriceSelection(requested_date, point, status, age, reason)


def classify_technical_account(
    account: Account,
    transactions: Iterable[RawTransaction],
) -> TechnicalAccountClassification:
    items = tuple(transactions)
    account_note = account.note.casefold()
    transaction_notes = " ".join(item.note for item in items).casefold()
    types = {item.raw_type for item in items}
    account_metadata_signal = "накопленн" in account_note and "купон" in account_note
    transaction_signal = "aci correction" in transaction_notes and "accrued" in transaction_notes
    type_profile = bool(items) and types <= {"INTEREST", "INTEREST_CHARGE"}
    name_signal = "aci" in account.name.casefold()
    is_technical = type_profile and (account_metadata_signal or transaction_signal)
    provenance = []
    if account_metadata_signal:
        provenance.append("ACCOUNT_NOTE_ACCRUED_COUPON")
    if transaction_signal:
        provenance.append("TRANSACTION_NOTE_ACCRUED_NOT_PAID")
    if type_profile:
        provenance.append("INTEREST_ONLY_TYPE_PROFILE")
    if name_signal:
        provenance.append("NAME_CORROBORATION_ONLY")
    return TechnicalAccountClassification(
        is_technical=is_technical,
        kind="ACCRUED_INTEREST" if is_technical else None,
        provenance=tuple(provenance),
        heuristic_used=is_technical,
    )


def build_valuation(
    model: PortfolioModel,
    normalized: NormalizationResult | None = None,
    *,
    valuation_date: date | None = None,
    stale_after_days: int = 31,
) -> ValuationResult:
    events = normalized or normalize_transactions(model)
    target_date = valuation_date or derive_valuation_date(model)
    warnings: list[str] = []

    normalized_account = events.account_cash_deltas()
    raw_account: dict[tuple[str, str], int] = {}
    unsupported_accounts: set[str] = set()
    transactions_by_account: dict[str, list[RawTransaction]] = {}
    for transaction in model.account_transactions:
        transactions_by_account.setdefault(transaction.owner_uuid, []).append(transaction)
        sign = cash_sign_for_type(transaction.raw_type)
        if sign is None:
            unsupported_accounts.add(transaction.owner_uuid)
            continue
        key = (transaction.owner_uuid, transaction.currency)
        raw_account[key] = raw_account.get(key, 0) + transaction.amount.raw * sign

    account_rows = []
    for account in model.accounts.values():
        key = (account.uuid, account.currency)
        expected = raw_account.get(key, 0)
        actual = normalized_account.get(key, 0)
        if account.uuid in unsupported_accounts:
            status = ReconciliationStatus.PARTIAL
            reason = "one or more transaction types have unknown cash sign"
        else:
            status = ReconciliationStatus.PASS if expected == actual else ReconciliationStatus.FAIL
            reason = None if status == ReconciliationStatus.PASS else "canonical cash delta differs from raw account legs"
        technical = classify_technical_account(account, transactions_by_account.get(account.uuid, ()))
        if technical.heuristic_used:
            warnings.append(
                f"technical account classification uses conservative heuristic: {account.uuid}"
            )
        account_rows.append(
            AccountValuation(
                account_uuid=account.uuid,
                name=account.name,
                currency=account.currency,
                balance=money_value(actual),
                retired=account.retired,
                transaction_count=len(account.transaction_uuids),
                technical=technical,
                reconciliation=Reconciliation(status, expected, actual, actual - expected, reason),
            )
        )

    normalized_quantities = events.portfolio_quantity_deltas()
    raw_quantities: dict[tuple[str, str], int] = {}
    unsupported_positions: set[tuple[str, str]] = set()
    for transaction in model.portfolio_transactions:
        if transaction.security_uuid is None:
            continue
        key = (transaction.owner_uuid, transaction.security_uuid)
        sign = quantity_sign_for_type(transaction.raw_type)
        if sign is None:
            unsupported_positions.add(key)
            continue
        raw_quantities[key] = raw_quantities.get(key, 0) + transaction.shares.raw * sign

    holding_rows = []
    closed_positions = []
    price_counts: dict[str, int] = {}
    for key in sorted(set(raw_quantities) | set(normalized_quantities)):
        portfolio_uuid, security_uuid = key
        expected = raw_quantities.get(key, 0)
        actual = normalized_quantities.get(key, 0)
        if key in unsupported_positions:
            reconciliation_status = ReconciliationStatus.PARTIAL
            reconciliation_reason = "one or more portfolio types have unknown quantity sign"
        else:
            reconciliation_status = ReconciliationStatus.PASS if expected == actual else ReconciliationStatus.FAIL
            reconciliation_reason = None if reconciliation_status == ReconciliationStatus.PASS else "canonical quantity differs from raw portfolio legs"
        reconciliation = Reconciliation(
            reconciliation_status, expected, actual, actual - expected, reconciliation_reason
        )
        if actual <= 0:
            if actual == 0:
                closed_positions.append(key)
            continue
        security = model.securities[security_uuid]
        portfolio = model.portfolios[portfolio_uuid]
        selection = select_price(security, target_date, stale_after_days=stale_after_days)
        price_counts[selection.status.value] = price_counts.get(selection.status.value, 0) + 1
        market_value = None
        if selection.point is not None:
            market_value = (
                share_value(actual).value * selection.point.value.value
            ).quantize(CENT, rounding=ROUND_HALF_UP)
        if selection.status in {PriceStatus.MISSING, PriceStatus.STALE}:
            warnings.append(
                f"{selection.status.value.lower()} price for security {security.uuid}: {selection.reason}"
            )
        holding_rows.append(
            HoldingValuation(
                security_uuid=security.uuid,
                security_name=security.name,
                isin=security.isin,
                portfolio_uuid=portfolio.uuid,
                portfolio_name=portfolio.name,
                quantity=share_value(actual),
                price=selection.point.value if selection.point else None,
                price_date=selection.point.day if selection.point else None,
                price_status=selection.status,
                price_age_days=selection.age_days,
                security_currency=security.currency,
                original_market_value=market_value,
                reconciliation=reconciliation,
            )
        )

    return ValuationResult(
        valuation_date=target_date,
        accounts=tuple(sorted(account_rows, key=lambda item: item.account_uuid)),
        holdings=tuple(sorted(holding_rows, key=lambda item: (item.portfolio_uuid, item.security_uuid))),
        closed_positions=tuple(sorted(closed_positions)),
        price_status_counts=price_counts,
        warnings=tuple(warnings),
    )


def _count(values: Iterable[str]) -> dict[str, int]:
    answer: dict[str, int] = {}
    for value in values:
        answer[value] = answer.get(value, 0) + 1
    return dict(sorted(answer.items()))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit Portfolio Performance valuation state")
    parser.add_argument("--audit", required=True, type=Path, metavar="XML")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        model = PortfolioModel.from_path(args.audit)
        result = build_valuation(model)
    except (OSError, ValueError) as error:
        sys.stderr.write(f"Portfolio valuation audit failed: {error}\n")
        return 2
    sys.stdout.write(json.dumps(result.audit_summary(), ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
