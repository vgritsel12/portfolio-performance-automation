#!/usr/bin/env python3
"""Secure, provenance-preserving reader for Portfolio Performance XML files.

The module parses the XStream object graph but does not calculate investment
performance. Identity is UUID-based, reference aliases resolve to canonical
element paths, and every fixed-point value retains its raw integer.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
from typing import Iterable, Mapping


MONEY_DIVIDER = 100
QUOTE_DIVIDER = 100_000_000
SHARE_DIVIDER = 100_000_000
WEIGHT_DIVIDER = 100
DEFAULT_MAX_XML_BYTES = 25 * 1024 * 1024
MIN_SUPPORTED_XML_VERSION = 52
CURRENT_SUPPORTED_XML_VERSION = 70
MAX_REFERENCE_COUNT = 100_000
MAX_REFERENCE_DEPTH = 128
MAX_REFERENCE_VALUE_CHARS = 512
MAX_REFERENCE_ID_CHARS = 128
MAX_REFERENCE_ISSUES = 64


class PortfolioModelError(ValueError):
    """Base class for actionable Portfolio Performance model errors."""


class XmlSecurityError(PortfolioModelError):
    """The input violates the secure XML or filesystem boundary."""


class UnsupportedVersionError(PortfolioModelError):
    """The XML version is outside the range accepted by the pinned engine."""


class UnresolvedReferenceError(PortfolioModelError):
    """A relative XStream reference has no target."""


class AmbiguousReferenceError(PortfolioModelError):
    """Canonical identity is ambiguous."""


class CyclicReferenceError(PortfolioModelError):
    """Reference aliases form a cycle."""


class ReferenceTypeError(PortfolioModelError):
    """A reference points at an element of the wrong semantic type."""


class ReferenceModeError(PortfolioModelError):
    """The document mixes or corrupts XStream reference modes."""


class TransactionNormalizationError(PortfolioModelError):
    """Raw transaction legs cannot be normalized without ambiguity."""


@dataclass(frozen=True)
class ScaledValue:
    raw: int
    divider: int
    kind: str

    @property
    def value(self) -> Decimal:
        return Decimal(self.raw) / Decimal(self.divider)

    def as_text(self) -> str:
        return format(self.value, "f")


def money_value(raw: int | str) -> ScaledValue:
    return ScaledValue(_integer(raw, "money"), MONEY_DIVIDER, "money")


def quote_value(raw: int | str) -> ScaledValue:
    return ScaledValue(_integer(raw, "quote"), QUOTE_DIVIDER, "quote")


def share_value(raw: int | str) -> ScaledValue:
    return ScaledValue(_integer(raw, "shares"), SHARE_DIVIDER, "shares")


def weight_value(raw: int | str) -> ScaledValue:
    return ScaledValue(_integer(raw, "weight"), WEIGHT_DIVIDER, "weight")


def rate_value(raw: int | str | Decimal) -> Decimal:
    try:
        value = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    except Exception as error:
        raise PortfolioModelError(f"rate is not a decimal: {raw!r}") from error
    if not value.is_finite():
        raise PortfolioModelError(f"rate is not finite: {raw!r}")
    return value


@dataclass(frozen=True)
class SourceLocation:
    path: str
    alias_path: str | None = None


@dataclass(frozen=True)
class PricePoint:
    day: date
    value: ScaledValue
    source: SourceLocation


@dataclass(frozen=True)
class Security:
    uuid: str
    name: str
    currency: str
    isin: str | None
    retired: bool
    prices: tuple[PricePoint, ...]
    source: SourceLocation


@dataclass(frozen=True)
class TransactionUnit:
    type: str
    currency: str
    amount: ScaledValue
    forex_currency: str | None
    forex_amount: ScaledValue | None
    exchange_rate: Decimal | None
    source: SourceLocation


@dataclass(frozen=True)
class RawTransaction:
    uuid: str
    owner_kind: str
    owner_uuid: str
    raw_type: str
    timestamp: datetime
    currency: str
    amount: ScaledValue
    shares: ScaledValue
    security_uuid: str | None
    note: str
    units: tuple[TransactionUnit, ...]
    cross_entry_path: str | None
    source: SourceLocation


@dataclass(frozen=True)
class Account:
    uuid: str
    name: str
    currency: str
    retired: bool
    note: str
    transaction_uuids: tuple[str, ...]
    source: SourceLocation


@dataclass(frozen=True)
class Portfolio:
    uuid: str
    name: str
    retired: bool
    reference_account_uuid: str | None
    transaction_uuids: tuple[str, ...]
    source: SourceLocation


@dataclass(frozen=True)
class TaxonomyAssignment:
    investment_vehicle_type: str
    investment_vehicle_uuid: str
    weight: ScaledValue
    rank: int
    source: SourceLocation

    @property
    def security_uuid(self) -> str | None:
        """Return the UUID only when the assignment targets a security."""
        return (
            self.investment_vehicle_uuid
            if self.investment_vehicle_type == "security"
            else None
        )


@dataclass(frozen=True)
class TaxonomyNode:
    id: str
    name: str
    color: str | None
    weight: ScaledValue
    rank: int
    data: tuple[tuple[str, str], ...]
    assignments: tuple[TaxonomyAssignment, ...]
    children: tuple["TaxonomyNode", ...]
    source: SourceLocation


@dataclass(frozen=True)
class Taxonomy:
    id: str
    name: str
    dimensions: tuple[str, ...]
    root: TaxonomyNode
    source: SourceLocation


@dataclass(frozen=True)
class Dashboard:
    id: str
    name: str
    source: SourceLocation


@dataclass(frozen=True)
class ClientFilterMember:
    vehicle_type: str
    vehicle_uuid: str
    weight_raw: int


@dataclass(frozen=True)
class ClientFilter:
    uuid: str
    name: str
    members: tuple[ClientFilterMember, ...]
    source: SourceLocation


@dataclass(frozen=True)
class GroupScope:
    id: str
    name: str
    filter_uuid: str | None
    members: tuple[ClientFilterMember, ...]
    account_uuids: tuple[str, ...]
    portfolio_uuids: tuple[str, ...]


@dataclass(frozen=True)
class ReferenceStats:
    total: int
    resolved: int
    unresolved: int = 0
    ambiguous: int = 0
    cyclic: int = 0
    type_mismatch: int = 0


@dataclass(frozen=True)
class ReferenceIssue:
    """Allow-listed structural detail for a reference-graph failure."""

    code: str
    mode: str
    source_kind: str
    source_class: str
    expected_type: str
    actual_type: str
    path_shape: str
    count: int = 1


@dataclass(frozen=True)
class ReferenceAudit:
    mode: str
    total: int
    resolved: int
    issue_occurrences: int
    issues: tuple[ReferenceIssue, ...]
    truncated: bool = False

    @property
    def valid(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class PortfolioModel:
    source_file: str
    version: int
    base_currency: str
    securities: Mapping[str, Security]
    accounts: Mapping[str, Account]
    portfolios: Mapping[str, Portfolio]
    taxonomies: tuple[Taxonomy, ...]
    dashboards: tuple[Dashboard, ...]
    client_filters: tuple[ClientFilter, ...]
    account_transactions: tuple[RawTransaction, ...]
    portfolio_transactions: tuple[RawTransaction, ...]
    references: Mapping[str, str]
    reference_stats: ReferenceStats

    @property
    def raw_transactions(self) -> tuple[RawTransaction, ...]:
        return self.account_transactions + self.portfolio_transactions

    @property
    def group_scopes(self) -> tuple[GroupScope, ...]:
        """Return full client plus saved Grouped Accounts scopes in saved order."""
        reference_accounts = {
            portfolio.reference_account_uuid
            for portfolio in self.portfolios.values()
            if portfolio.reference_account_uuid is not None
        }
        full_members = tuple(
            ClientFilterMember("account", account.uuid, 10000)
            for account in sorted(self.accounts.values(), key=lambda item: item.uuid)
            if account.uuid not in reference_accounts
        ) + tuple(
            ClientFilterMember("portfolio", portfolio.uuid, 10000)
            for portfolio in sorted(self.portfolios.values(), key=lambda item: item.uuid)
        )
        scopes = [
            GroupScope(
                id="FULL_XML",
                name="Весь портфель",
                filter_uuid=None,
                members=full_members,
                account_uuids=tuple(sorted(self.accounts)),
                portfolio_uuids=tuple(sorted(self.portfolios)),
            )
        ]
        for group in self.client_filters:
            if any(
                member.vehicle_uuid
                not in (self.accounts if member.vehicle_type == "account" else self.portfolios)
                for member in group.members
            ):
                continue
            accounts = {
                member.vehicle_uuid
                for member in group.members
                if member.vehicle_type == "account"
            }
            portfolios = {
                member.vehicle_uuid
                for member in group.members
                if member.vehicle_type == "portfolio"
            }
            accounts.update(
                self.portfolios[portfolio_uuid].reference_account_uuid
                for portfolio_uuid in portfolios
                if self.portfolios[portfolio_uuid].reference_account_uuid is not None
            )
            scopes.append(
                GroupScope(
                    id=group.uuid,
                    name=group.name,
                    filter_uuid=group.uuid,
                    members=group.members,
                    account_uuids=tuple(sorted(accounts)),
                    portfolio_uuids=tuple(sorted(portfolios)),
                )
            )
        return tuple(scopes)

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        allowed_root: str | Path | None = None,
        max_bytes: int = DEFAULT_MAX_XML_BYTES,
    ) -> "PortfolioModel":
        xml_path, root = _secure_load(path, allowed_root=allowed_root, max_bytes=max_bytes)
        return _ModelReader(xml_path, root).read()

    def audit_summary(self) -> dict[str, object]:
        duplicate_names: dict[str, int] = {}
        for security in self.securities.values():
            duplicate_names[security.name] = duplicate_names.get(security.name, 0) + 1
        normalized = normalize_transactions(self)
        return {
            "source_file": self.source_file,
            "version": self.version,
            "base_currency": self.base_currency,
            "entities": {
                "securities": len(self.securities),
                "accounts": len(self.accounts),
                "portfolios": len(self.portfolios),
                "taxonomies": len(self.taxonomies),
                "dashboards": len(self.dashboards),
            },
            "transactions": {
                "account": len(self.account_transactions),
                "portfolio": len(self.portfolio_transactions),
                "raw_legs": len(self.raw_transactions),
                "canonical_events": len(normalized.events),
                "paired_trade_events": normalized.paired_trade_count,
                "unit_types": _count_values(unit.type for tx in self.raw_transactions for unit in tx.units),
            },
            "currencies": sorted(
                {self.base_currency}
                | {item.currency for item in self.securities.values()}
                | {item.currency for item in self.accounts.values()}
            ),
            "references": self.reference_stats.__dict__,
            "duplicate_security_names": {
                name: count for name, count in duplicate_names.items() if count > 1
            },
            "historical_prices": sum(len(item.prices) for item in self.securities.values()),
        }


class DisplayCategory(str, Enum):
    TRADE = "TRADE"
    CASH_FLOW = "CASH_FLOW"
    DIVIDENDS = "DIVIDENDS"
    INTEREST = "INTEREST"
    INTEREST_CHARGE = "INTEREST_CHARGE"
    TRANSFER = "TRANSFER"
    FEE = "FEE"
    TAX = "TAX"
    OTHER = "OTHER"


class TransferStatus(str, Enum):
    NOT_TRANSFER = "NOT_TRANSFER"
    LINKED = "LINKED"
    UNCERTAIN_NOTE = "UNCERTAIN_NOTE"


@dataclass(frozen=True)
class EventLeg:
    leg_uuid: str
    owner_kind: str
    owner_uuid: str
    raw_type: str
    source: SourceLocation


@dataclass(frozen=True)
class CashImpact:
    account_uuid: str
    currency: str
    amount: ScaledValue
    source_leg_uuid: str


@dataclass(frozen=True)
class QuantityImpact:
    portfolio_uuid: str
    security_uuid: str
    shares: ScaledValue
    source_leg_uuid: str


@dataclass(frozen=True)
class CanonicalEvent:
    event_id: str
    timestamp: datetime
    canonical_type: str
    raw_types: tuple[str, ...]
    display_category: DisplayCategory
    display_label: str
    security_uuids: tuple[str, ...]
    account_uuids: tuple[str, ...]
    portfolio_uuids: tuple[str, ...]
    cash_impacts: tuple[CashImpact, ...]
    quantity_impacts: tuple[QuantityImpact, ...]
    units: tuple[TransactionUnit, ...]
    notes: tuple[str, ...]
    display_hints: tuple[str, ...]
    transfer_status: TransferStatus
    legs: tuple[EventLeg, ...]

    @property
    def fee_units(self) -> tuple[TransactionUnit, ...]:
        return tuple(unit for unit in self.units if unit.type == "FEE")

    @property
    def tax_units(self) -> tuple[TransactionUnit, ...]:
        return tuple(unit for unit in self.units if unit.type == "TAX")


@dataclass(frozen=True)
class NormalizationResult:
    events: tuple[CanonicalEvent, ...]
    raw_leg_count: int
    paired_trade_count: int
    linked_transfer_count: int

    def account_cash_deltas(self) -> dict[tuple[str, str], int]:
        answer: dict[tuple[str, str], int] = {}
        for event in self.events:
            for impact in event.cash_impacts:
                key = (impact.account_uuid, impact.currency)
                answer[key] = answer.get(key, 0) + impact.amount.raw
        return answer

    def portfolio_quantity_deltas(self) -> dict[tuple[str, str], int]:
        answer: dict[tuple[str, str], int] = {}
        for event in self.events:
            for impact in event.quantity_impacts:
                key = (impact.portfolio_uuid, impact.security_uuid)
                answer[key] = answer.get(key, 0) + impact.shares.raw
        return answer

    def audit_summary(self) -> dict[str, object]:
        return {
            "raw_legs": self.raw_leg_count,
            "canonical_events": len(self.events),
            "paired_trade_events": self.paired_trade_count,
            "linked_transfer_events": self.linked_transfer_count,
            "canonical_types": _count_values(event.canonical_type for event in self.events),
            "display_categories": _count_values(event.display_category.value for event in self.events),
            "unit_types": _count_values(unit.type for event in self.events for unit in event.units),
            "unit_source_paths_unique": len(
                {unit.source.path for event in self.events for unit in event.units}
            ),
            "uncertain_transfer_events": sum(
                event.transfer_status == TransferStatus.UNCERTAIN_NOTE
                for event in self.events
            ),
        }


_ACCOUNT_CASH_SIGNS = {
    "BUY": -1,
    "SELL": 1,
    "DEPOSIT": 1,
    "REMOVAL": -1,
    "DIVIDENDS": 1,
    "INTEREST": 1,
    "INTEREST_CHARGE": -1,
    "TAX_REFUND": 1,
    "FEES_REFUND": 1,
    "TAXES": -1,
    "FEES": -1,
    "TRANSFER_IN": 1,
    "TRANSFER_OUT": -1,
}

_PORTFOLIO_SHARE_SIGNS = {
    "BUY": 1,
    "SELL": -1,
    "TRANSFER_IN": 1,
    "TRANSFER_OUT": -1,
    "DELIVERY_INBOUND": 1,
    "DELIVERY_OUTBOUND": -1,
}


def cash_sign_for_type(raw_type: str) -> int | None:
    return _ACCOUNT_CASH_SIGNS.get(raw_type)


def quantity_sign_for_type(raw_type: str) -> int | None:
    return _PORTFOLIO_SHARE_SIGNS.get(raw_type)


def normalize_transactions(model: PortfolioModel) -> NormalizationResult:
    """Create one economic event per linked operation without name/date joins."""
    portfolio_by_cross: dict[str, list[RawTransaction]] = {}
    for transaction in model.portfolio_transactions:
        if transaction.cross_entry_path:
            portfolio_by_cross.setdefault(transaction.cross_entry_path, []).append(transaction)

    account_transfer_groups: dict[str, list[RawTransaction]] = {}
    for transaction in model.account_transactions:
        if transaction.raw_type in {"TRANSFER_IN", "TRANSFER_OUT"} and transaction.cross_entry_path:
            account_transfer_groups.setdefault(transaction.cross_entry_path, []).append(transaction)

    events: list[CanonicalEvent] = []
    used_account: set[str] = set()
    used_portfolio: set[str] = set()
    paired_trades = 0
    linked_transfers = 0

    for transaction in model.account_transactions:
        if transaction.uuid in used_account:
            continue
        if transaction.raw_type in {"TRANSFER_IN", "TRANSFER_OUT"} and transaction.cross_entry_path:
            group = account_transfer_groups.get(transaction.cross_entry_path, [])
            if len(group) == 2 and {item.raw_type for item in group} == {"TRANSFER_IN", "TRANSFER_OUT"}:
                events.append(_build_event(group, (), canonical_type="TRANSFER", transfer_status=TransferStatus.LINKED))
                used_account.update(item.uuid for item in group)
                linked_transfers += 1
                continue

        portfolio_legs: tuple[RawTransaction, ...] = ()
        if transaction.raw_type in {"BUY", "SELL"}:
            matches = [
                item
                for item in portfolio_by_cross.get(transaction.cross_entry_path or "", [])
                if item.raw_type == transaction.raw_type and item.uuid not in used_portfolio
            ]
            if len(matches) != 1:
                raise TransactionNormalizationError(
                    f"{transaction.raw_type} account leg {transaction.uuid} has {len(matches)} matching portfolio legs"
                )
            portfolio_legs = (matches[0],)
            used_portfolio.add(matches[0].uuid)
            paired_trades += 1
        events.append(_build_event((transaction,), portfolio_legs))
        used_account.add(transaction.uuid)

    remaining = [item for item in model.portfolio_transactions if item.uuid not in used_portfolio]
    remaining_by_cross: dict[str, list[RawTransaction]] = {}
    for transaction in remaining:
        if transaction.raw_type in {"BUY", "SELL"}:
            raise TransactionNormalizationError(
                f"unmatched {transaction.raw_type} portfolio leg {transaction.uuid}"
            )
        if transaction.cross_entry_path:
            remaining_by_cross.setdefault(transaction.cross_entry_path, []).append(transaction)

    consumed_remaining: set[str] = set()
    for transaction in remaining:
        if transaction.uuid in consumed_remaining:
            continue
        if transaction.raw_type in {"TRANSFER_IN", "TRANSFER_OUT"} and transaction.cross_entry_path:
            group = remaining_by_cross.get(transaction.cross_entry_path, [])
            if len(group) == 2 and {item.raw_type for item in group} == {"TRANSFER_IN", "TRANSFER_OUT"}:
                events.append(_build_event((), group, canonical_type="TRANSFER", transfer_status=TransferStatus.LINKED))
                consumed_remaining.update(item.uuid for item in group)
                linked_transfers += 1
                continue
        events.append(_build_event((), (transaction,)))
        consumed_remaining.add(transaction.uuid)

    events.sort(key=lambda item: (item.timestamp, item.event_id))
    return NormalizationResult(
        events=tuple(events),
        raw_leg_count=len(model.raw_transactions),
        paired_trade_count=paired_trades,
        linked_transfer_count=linked_transfers,
    )


def _build_event(
    account_legs: Iterable[RawTransaction],
    portfolio_legs: Iterable[RawTransaction],
    *,
    canonical_type: str | None = None,
    transfer_status: TransferStatus | None = None,
) -> CanonicalEvent:
    account = tuple(account_legs)
    portfolio = tuple(portfolio_legs)
    raw = account + portfolio
    if not raw:
        raise TransactionNormalizationError("cannot build an event without transaction legs")
    timestamps = {item.timestamp for item in raw}
    if len(timestamps) != 1:
        raise TransactionNormalizationError(
            "linked transaction legs have different timestamps: "
            + ", ".join(sorted(item.timestamp.isoformat() for item in raw))
        )
    raw_types = tuple(dict.fromkeys(item.raw_type for item in raw))
    selected_type = canonical_type or account[0].raw_type if account else canonical_type or portfolio[0].raw_type
    if selected_type is None:
        raise TransactionNormalizationError("canonical transaction type is unavailable")
    category, label = _display_category(selected_type)

    cash_impacts = tuple(
        CashImpact(
            account_uuid=item.owner_uuid,
            currency=item.currency,
            amount=_signed(item.amount, _ACCOUNT_CASH_SIGNS[item.raw_type]),
            source_leg_uuid=item.uuid,
        )
        for item in account
        if item.raw_type in _ACCOUNT_CASH_SIGNS
    )
    quantity_impacts = tuple(
        QuantityImpact(
            portfolio_uuid=item.owner_uuid,
            security_uuid=item.security_uuid,
            shares=_signed(item.shares, _PORTFOLIO_SHARE_SIGNS[item.raw_type]),
            source_leg_uuid=item.uuid,
        )
        for item in portfolio
        if item.raw_type in _PORTFOLIO_SHARE_SIGNS and item.security_uuid is not None
    )
    units_by_path: dict[str, TransactionUnit] = {}
    for item in raw:
        for unit in item.units:
            units_by_path.setdefault(unit.source.path, unit)
    notes = tuple(dict.fromkeys(item.note for item in raw if item.note))
    hints = _display_hints(notes, selected_type)
    if transfer_status is None:
        transfer_status = (
            TransferStatus.UNCERTAIN_NOTE
            if "POSSIBLE_TRANSFER_FROM_NOTE" in hints
            else TransferStatus.NOT_TRANSFER
        )
    ids = sorted(item.uuid for item in raw)
    event_id = ids[0] if len(account) == 1 else "xentry:" + ":".join(ids)
    return CanonicalEvent(
        event_id=event_id,
        timestamp=next(iter(timestamps)),
        canonical_type=selected_type,
        raw_types=raw_types,
        display_category=category,
        display_label=label,
        security_uuids=tuple(sorted({item.security_uuid for item in raw if item.security_uuid})),
        account_uuids=tuple(sorted({item.owner_uuid for item in account})),
        portfolio_uuids=tuple(sorted({item.owner_uuid for item in portfolio})),
        cash_impacts=cash_impacts,
        quantity_impacts=quantity_impacts,
        units=tuple(units_by_path[path] for path in sorted(units_by_path)),
        notes=notes,
        display_hints=hints,
        transfer_status=transfer_status,
        legs=tuple(
            EventLeg(
                leg_uuid=item.uuid,
                owner_kind=item.owner_kind,
                owner_uuid=item.owner_uuid,
                raw_type=item.raw_type,
                source=item.source,
            )
            for item in raw
        ),
    )


def _signed(value: ScaledValue, sign: int) -> ScaledValue:
    return ScaledValue(value.raw * sign, value.divider, value.kind)


def _display_category(raw_type: str) -> tuple[DisplayCategory, str]:
    known = {
        "BUY": (DisplayCategory.TRADE, "Buy"),
        "SELL": (DisplayCategory.TRADE, "Sell"),
        "DEPOSIT": (DisplayCategory.CASH_FLOW, "Deposit"),
        "REMOVAL": (DisplayCategory.CASH_FLOW, "Removal"),
        "DIVIDENDS": (DisplayCategory.DIVIDENDS, "Dividends"),
        "INTEREST": (DisplayCategory.INTEREST, "Interest"),
        "INTEREST_CHARGE": (DisplayCategory.INTEREST_CHARGE, "Interest charge"),
        "TRANSFER": (DisplayCategory.TRANSFER, "Internal transfer"),
        "TRANSFER_IN": (DisplayCategory.TRANSFER, "Transfer in"),
        "TRANSFER_OUT": (DisplayCategory.TRANSFER, "Transfer out"),
        "FEES": (DisplayCategory.FEE, "Fee"),
        "TAXES": (DisplayCategory.TAX, "Tax"),
    }
    return known.get(raw_type, (DisplayCategory.OTHER, f"Other / {raw_type}"))


def _display_hints(notes: Iterable[str], raw_type: str) -> tuple[str, ...]:
    if raw_type != "REMOVAL":
        return ()
    combined = " ".join(notes).casefold()
    hints = []
    if any(token in combined for token in ("custody fee", "commission", "kommission")):
        hints.append("POSSIBLE_OTHER_CHARGE_FROM_NOTE")
    if "->" in combined or "funds transfer" in combined:
        hints.append("POSSIBLE_TRANSFER_FROM_NOTE")
    return tuple(hints)


class _ReferenceResolver:
    """Resolve and audit the bounded XStream object graph.

    XStream supports both XPath references and ID/reference tokens. The document
    selects one mode through its reference values; mixing the modes is rejected
    because silently guessing can connect a transaction to the wrong object.
    """

    _SOURCE_ALIASES = {
        "referenceaccount": "account",
        "accountfrom": "account",
        "accountto": "account",
        "portfoliofrom": "portfolio",
        "portfolioto": "portfolio",
        "portfoliotransaction": "portfoliotransaction",
        "accounttransaction": "accounttransaction",
    }
    _TARGET_ALIASES = {
        "accountfrom": "account",
        "accountto": "account",
        "portfoliofrom": "portfolio",
        "portfolioto": "portfolio",
    }
    _SAFE_KINDS = {
        "account",
        "accounts",
        "accounttransaction",
        "client",
        "classification",
        "crossentry",
        "dashboards",
        "investmentvehicle",
        "parent",
        "portfolio",
        "portfolios",
        "portfoliotransaction",
        "referenceaccount",
        "references",
        "root",
        "securities",
        "security",
        "targets",
        "taxonomies",
        "transactionfrom",
        "transactions",
        "transactionto",
    }
    _SAFE_CLASSES = {"account", "buysell", "security"}
    _ERROR_CODE = {
        ReferenceModeError: "INPUT_REFERENCE_MODE",
        AmbiguousReferenceError: "INPUT_REFERENCE_AMBIGUOUS",
        CyclicReferenceError: "INPUT_REFERENCE_CYCLIC",
        UnresolvedReferenceError: "INPUT_REFERENCE_UNRESOLVED",
        ReferenceTypeError: "INPUT_REFERENCE_TYPE",
    }
    _ERROR_PRIORITY = {
        "INPUT_REFERENCE_MODE": 0,
        "INPUT_REFERENCE_AMBIGUOUS": 1,
        "INPUT_REFERENCE_CYCLIC": 2,
        "INPUT_REFERENCE_UNRESOLVED": 3,
        "INPUT_REFERENCE_TYPE": 4,
    }

    def __init__(self, root: ET.Element):
        self.root = root
        self.parent = {child: parent for parent in root.iter() for child in parent}
        self.path_by_element: dict[ET.Element, str] = {}
        self.element_by_path: dict[str, ET.Element] = {}
        self._index_paths(root, f"/{root.tag}")
        self.reference_elements = tuple(
            element for element in root.iter() if "reference" in element.attrib
        )
        if len(self.reference_elements) > MAX_REFERENCE_COUNT:
            raise XmlSecurityError("XML reference count exceeds the safety limit")
        self.elements_by_id: dict[str, list[ET.Element]] = {}
        self.invalid_id_count = 0
        for element in root.iter():
            identifier = element.attrib.get("id")
            if identifier is None:
                continue
            if not self._valid_id(identifier):
                self.invalid_id_count += 1
                continue
            self.elements_by_id.setdefault(identifier, []).append(element)
        self.resolved_paths: dict[str, str] = {}
        self.mode = self._detect_mode()

    def _index_paths(self, element: ET.Element, path: str) -> None:
        if len(path) > MAX_REFERENCE_VALUE_CHARS * 2:
            raise XmlSecurityError("XML structural path exceeds the safety limit")
        if path in self.element_by_path:
            raise AmbiguousReferenceError("duplicate canonical XML path")
        self.path_by_element[element] = path
        self.element_by_path[path] = element
        counts: dict[str, int] = {}
        for child in element:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            index = counts[child.tag]
            segment = child.tag if index == 1 else f"{child.tag}[{index}]"
            self._index_paths(child, f"{path}/{segment}")

    @staticmethod
    def _valid_id(value: str) -> bool:
        return bool(value) and len(value) <= MAX_REFERENCE_ID_CHARS and not any(
            character.isspace() or ord(character) < 32 for character in value
        )

    @staticmethod
    def _reference_style(value: str) -> str:
        return "path" if value.startswith((".", "/")) else "id"

    def _detect_mode(self) -> str:
        if not self.reference_elements:
            return "none"
        styles = {
            self._reference_style(element.attrib["reference"])
            for element in self.reference_elements
            if element.attrib["reference"]
        }
        if len(styles) != 1:
            return "mixed"
        return next(iter(styles))

    def path(self, element: ET.Element) -> str:
        return self.path_by_element[element]

    def resolve(self, element: ET.Element) -> ET.Element:
        return self._resolve(element, ())

    def _resolve(self, element: ET.Element, stack: tuple[str, ...]) -> ET.Element:
        reference = element.attrib.get("reference")
        if reference is None:
            return element
        source_path = self.path(element)
        if self.mode == "mixed":
            raise ReferenceModeError("mixed or invalid XML reference mode")
        if (
            not reference
            or len(reference) > MAX_REFERENCE_VALUE_CHARS
            or any(ord(character) < 32 for character in reference)
        ):
            raise ReferenceModeError("invalid XML reference token")
        if source_path in stack:
            raise CyclicReferenceError(
                f"cyclic XML reference at {self._safe_path_shape(source_path)}"
            )
        if len(stack) >= MAX_REFERENCE_DEPTH:
            raise ReferenceModeError("XML reference depth exceeds the safety limit")
        target = self._locate_target(source_path, reference)
        resolved = self._resolve(target, (*stack, source_path))
        self._validate_type(element, resolved, source_path)
        self.resolved_paths[source_path] = self.path(resolved)
        return resolved

    def _locate_target(self, source_path: str, reference: str) -> ET.Element:
        if self.mode == "id":
            if not self._valid_id(reference):
                raise ReferenceModeError("invalid XML ID reference token")
            candidates = self.elements_by_id.get(reference, [])
            if not candidates:
                raise UnresolvedReferenceError(
                    f"unresolved XML reference at {self._safe_path_shape(source_path)}"
                )
            if len(candidates) != 1:
                raise AmbiguousReferenceError(
                    f"ambiguous XML reference at {self._safe_path_shape(source_path)}"
                )
            return candidates[0]
        target_path = self._target_path(source_path, reference)
        target = self.element_by_path.get(target_path)
        if target is None:
            raise UnresolvedReferenceError(
                f"unresolved XML reference at {self._safe_path_shape(source_path)}"
            )
        return target

    @staticmethod
    def _canonical_segment(token: str) -> str:
        match = re.fullmatch(r"([^/\[\]]+)\[(\d+)\]", token)
        if match is None:
            return token
        return match.group(1) if match.group(2) == "1" else token

    @classmethod
    def _target_path(cls, source_path: str, reference: str) -> str:
        if reference.startswith("/"):
            parts: list[str] = []
        else:
            parts = [part for part in source_path.split("/") if part]
        for raw_token in reference.split("/"):
            token = cls._canonical_segment(raw_token)
            if token in ("", "."):
                continue
            if token == "..":
                if not parts:
                    raise UnresolvedReferenceError(
                        f"unresolved XML reference at {cls._safe_path_shape(source_path)}"
                    )
                parts.pop()
            else:
                parts.append(token)
        return "/" + "/".join(parts)

    @staticmethod
    def _normalize_type(value: str | None) -> str:
        return re.sub(r"[^a-z]", "", (value or "").lower())

    @classmethod
    def _expected_types(cls, source: ET.Element) -> frozenset[str]:
        source_type = cls._normalize_type(source.tag)
        if source_type == "parent":
            return frozenset({"*"})
        if source_type == "investmentvehicle":
            return frozenset({cls._normalize_type(source.attrib.get("class"))})
        if source_type in {"transactionfrom", "transactionto"}:
            return frozenset({"accounttransaction", "portfoliotransaction"})
        return frozenset({cls._SOURCE_ALIASES.get(source_type, source_type)})

    @classmethod
    def _actual_type(cls, target: ET.Element) -> str:
        target_type = cls._normalize_type(target.tag)
        return cls._TARGET_ALIASES.get(target_type, target_type)

    @classmethod
    def _validate_type(cls, source: ET.Element, target: ET.Element, path: str) -> None:
        expected = cls._expected_types(source)
        actual = cls._actual_type(target)
        if "*" not in expected and actual not in expected:
            raise ReferenceTypeError(
                f"reference type mismatch at {cls._safe_path_shape(path)}"
            )

    @classmethod
    def _safe_kind(cls, value: str | None) -> str:
        normalized = cls._normalize_type(value)
        return normalized if normalized in cls._SAFE_KINDS else "other"

    @classmethod
    def _safe_class(cls, value: str | None) -> str:
        normalized = cls._normalize_type(value)
        return normalized if normalized in cls._SAFE_CLASSES else "none"

    @classmethod
    def _safe_path_shape(cls, path: str) -> str:
        shapes = []
        for segment in (part for part in path.split("/") if part):
            tag = re.sub(r"\[\d+\]$", "", segment)
            safe = cls._safe_kind(tag)
            shapes.append(safe)
        return "/" + "/".join(shapes[-12:])

    def _issue_for(self, element: ET.Element, error: PortfolioModelError) -> ReferenceIssue:
        source_path = self.path(element)
        actual = "unavailable"
        try:
            target = self._locate_target(source_path, element.attrib.get("reference", ""))
            actual = self._safe_kind(target.tag)
        except PortfolioModelError:
            pass
        expected_values = self._expected_types(element)
        expected = "+".join(sorted(expected_values))
        if expected == "*":
            expected = "any"
        expected = expected if expected and expected != "" else "unavailable"
        code = next(
            (
                stable
                for error_type, stable in self._ERROR_CODE.items()
                if isinstance(error, error_type)
            ),
            "INPUT_REFERENCE_MODE",
        )
        return ReferenceIssue(
            code=code,
            mode=self.mode,
            source_kind=self._safe_kind(element.tag),
            source_class=self._safe_class(element.attrib.get("class")),
            expected_type=expected,
            actual_type=actual,
            path_shape=self._safe_path_shape(source_path),
        )

    def audit(self) -> ReferenceAudit:
        raw_issues: list[ReferenceIssue] = []
        if self.mode == "mixed":
            raw_issues.append(
                ReferenceIssue(
                    code="INPUT_REFERENCE_MODE",
                    mode="mixed",
                    source_kind="other",
                    source_class="none",
                    expected_type="single-mode",
                    actual_type="mixed",
                    path_shape="/client/*",
                    count=len(self.reference_elements),
                )
            )
        elif self.invalid_id_count and self.mode == "id":
            raw_issues.append(
                ReferenceIssue(
                    code="INPUT_REFERENCE_MODE",
                    mode="id",
                    source_kind="other",
                    source_class="none",
                    expected_type="valid-id",
                    actual_type="invalid-id",
                    path_shape="/client/*",
                    count=self.invalid_id_count,
                )
            )
        else:
            for element in self.reference_elements:
                try:
                    self.resolve(element)
                except (ReferenceModeError, AmbiguousReferenceError, CyclicReferenceError,
                        UnresolvedReferenceError, ReferenceTypeError) as error:
                    raw_issues.append(self._issue_for(element, error))

        grouped: dict[tuple[str, ...], int] = {}
        for issue in raw_issues:
            key = (
                issue.code,
                issue.mode,
                issue.source_kind,
                issue.source_class,
                issue.expected_type,
                issue.actual_type,
                issue.path_shape,
            )
            grouped[key] = grouped.get(key, 0) + issue.count
        rows = [
            ReferenceIssue(*key, count=count)
            for key, count in sorted(
                grouped.items(),
                key=lambda item: (self._ERROR_PRIORITY.get(item[0][0], 99), item[0]),
            )[:MAX_REFERENCE_ISSUES]
        ]
        occurrence_count = sum(grouped.values())
        return ReferenceAudit(
            mode=self.mode,
            total=len(self.reference_elements),
            resolved=len(self.resolved_paths),
            issue_occurrences=occurrence_count,
            issues=tuple(rows),
            truncated=len(grouped) > MAX_REFERENCE_ISSUES,
        )

    @classmethod
    def raise_for_audit(cls, audit: ReferenceAudit) -> None:
        if audit.valid:
            return
        issue = audit.issues[0]
        error_type = {
            "INPUT_REFERENCE_MODE": ReferenceModeError,
            "INPUT_REFERENCE_AMBIGUOUS": AmbiguousReferenceError,
            "INPUT_REFERENCE_CYCLIC": CyclicReferenceError,
            "INPUT_REFERENCE_UNRESOLVED": UnresolvedReferenceError,
            "INPUT_REFERENCE_TYPE": ReferenceTypeError,
        }[issue.code]
        label = {
            "INPUT_REFERENCE_MODE": "mixed or invalid XML reference mode",
            "INPUT_REFERENCE_AMBIGUOUS": "ambiguous XML reference",
            "INPUT_REFERENCE_CYCLIC": "cyclic XML reference",
            "INPUT_REFERENCE_UNRESOLVED": "unresolved XML reference",
            "INPUT_REFERENCE_TYPE": "reference type mismatch",
        }[issue.code]
        error = error_type(f"{label} at {issue.path_shape}")
        error.reference_audit = audit  # type: ignore[attr-defined]
        raise error


class _ModelReader:
    def __init__(self, source: Path, root: ET.Element):
        self.source = source
        self.root = root
        self.resolver = _ReferenceResolver(root)

    def read(self) -> PortfolioModel:
        if self.root.tag != "client":
            raise PortfolioModelError(f"expected <client> root, got <{self.root.tag}>")
        version = _required_int(self.root, "version", "/client")
        if not MIN_SUPPORTED_XML_VERSION <= version <= CURRENT_SUPPORTED_XML_VERSION:
            raise UnsupportedVersionError(
                "Portfolio XML version is outside the supported range "
                f"{MIN_SUPPORTED_XML_VERSION}..{CURRENT_SUPPORTED_XML_VERSION}"
            )
        base_currency = _required_text(self.root, "baseCurrency", "/client").upper()

        reference_audit = self.resolver.audit()
        self.resolver.raise_for_audit(reference_audit)

        securities = self._read_securities()
        account_transactions: list[RawTransaction] = []
        accounts = self._read_accounts(account_transactions)
        portfolio_transactions: list[RawTransaction] = []
        portfolios = self._read_portfolios(portfolio_transactions)
        taxonomies = self._read_taxonomies()
        dashboards = self._read_dashboards()
        client_filters = self._read_client_filters(accounts, portfolios)

        _require_unique_transactions(account_transactions, "account")
        _require_unique_transactions(portfolio_transactions, "portfolio")
        return PortfolioModel(
            source_file=self.source.name,
            version=version,
            base_currency=base_currency,
            securities=securities,
            accounts=accounts,
            portfolios=portfolios,
            taxonomies=taxonomies,
            dashboards=dashboards,
            client_filters=client_filters,
            account_transactions=tuple(account_transactions),
            portfolio_transactions=tuple(portfolio_transactions),
            references=dict(sorted(self.resolver.resolved_paths.items())),
            reference_stats=ReferenceStats(
                total=reference_audit.total,
                resolved=reference_audit.resolved,
            ),
        )

    def _read_securities(self) -> dict[str, Security]:
        container = self.root.find("securities")
        if container is None:
            raise PortfolioModelError("/client/securities is missing")
        answer: dict[str, Security] = {}
        for element in container.findall("security"):
            if "reference" in element.attrib:
                continue
            path = self.resolver.path(element)
            uuid = _required_text(element, "uuid", path)
            _insert_unique(answer, uuid, path, "security")
            prices = []
            prices_element = element.find("prices")
            if prices_element is not None:
                for price in prices_element.findall("price"):
                    price_path = self.resolver.path(price)
                    prices.append(
                        PricePoint(
                            day=_date(price.attrib.get("t"), f"{price_path}@t"),
                            value=quote_value(_required_attribute(price, "v", price_path)),
                            source=SourceLocation(price_path),
                        )
                    )
            prices.sort(key=lambda item: item.day)
            answer[uuid] = Security(
                uuid=uuid,
                name=_required_text(element, "name", path),
                currency=_required_text(element, "currencyCode", path).upper(),
                isin=_optional_text(element, "isin"),
                retired=_boolean(_optional_text(element, "isRetired") or "false", path),
                prices=tuple(prices),
                source=SourceLocation(path),
            )
        return answer

    def _read_accounts(self, transactions: list[RawTransaction]) -> dict[str, Account]:
        container = self.root.find("accounts")
        if container is None:
            raise PortfolioModelError("/client/accounts is missing")
        answer: dict[str, Account] = {}
        for alias in container.findall("account"):
            element = self.resolver.resolve(alias)
            path = self.resolver.path(element)
            alias_path = self.resolver.path(alias)
            uuid = _required_text(element, "uuid", path)
            if uuid in answer and answer[uuid].source.path == path:
                continue
            _insert_unique(answer, uuid, path, "account")
            currency = _required_text(element, "currencyCode", path).upper()
            transaction_uuids: list[str] = []
            tx_container = element.find("transactions")
            if tx_container is not None:
                for alias in list(tx_container):
                    target = self.resolver.resolve(alias)
                    tx = self._read_transaction(
                        target,
                        alias,
                        owner_kind="ACCOUNT",
                        owner_uuid=uuid,
                        default_currency=currency,
                    )
                    transactions.append(tx)
                    transaction_uuids.append(tx.uuid)
            answer[uuid] = Account(
                uuid=uuid,
                name=_required_text(element, "name", path),
                currency=currency,
                retired=_boolean(_optional_text(element, "isRetired") or "false", path),
                note=_optional_text(element, "note") or "",
                transaction_uuids=tuple(transaction_uuids),
                source=SourceLocation(path, alias_path if alias_path != path else None),
            )
        return answer

    def _read_portfolios(self, transactions: list[RawTransaction]) -> dict[str, Portfolio]:
        full_objects = [
            element
            for element in self.root.iter()
            if element.tag in {"portfolio", "portfolioFrom", "portfolioTo"}
            if "reference" not in element.attrib and element.find("uuid") is not None
        ]
        answer: dict[str, Portfolio] = {}
        for element in full_objects:
            path = self.resolver.path(element)
            uuid = _required_text(element, "uuid", path)
            if uuid in answer:
                raise AmbiguousReferenceError(
                    f"duplicate portfolio UUID {uuid!r} at {answer[uuid].source.path} and {path}"
                )
            reference_account_uuid = None
            reference_account = element.find("referenceAccount")
            default_currency = ""
            if reference_account is not None:
                target = self.resolver.resolve(reference_account)
                reference_account_uuid = _required_text(
                    target, "uuid", self.resolver.path(target)
                )
                default_currency = _required_text(
                    target, "currencyCode", self.resolver.path(target)
                ).upper()
            transaction_uuids: list[str] = []
            tx_container = element.find("transactions")
            if tx_container is not None:
                for alias in list(tx_container):
                    target = self.resolver.resolve(alias)
                    tx = self._read_transaction(
                        target,
                        alias,
                        owner_kind="PORTFOLIO",
                        owner_uuid=uuid,
                        default_currency=default_currency,
                    )
                    transactions.append(tx)
                    transaction_uuids.append(tx.uuid)
            answer[uuid] = Portfolio(
                uuid=uuid,
                name=_required_text(element, "name", path),
                retired=_boolean(_optional_text(element, "isRetired") or "false", path),
                reference_account_uuid=reference_account_uuid,
                transaction_uuids=tuple(transaction_uuids),
                source=SourceLocation(path),
            )
        return answer

    def _read_transaction(
        self,
        element: ET.Element,
        alias: ET.Element,
        *,
        owner_kind: str,
        owner_uuid: str,
        default_currency: str,
    ) -> RawTransaction:
        path = self.resolver.path(element)
        alias_path = self.resolver.path(alias)
        security_uuid = None
        security = element.find("security")
        if security is not None:
            target = self.resolver.resolve(security)
            security_uuid = _required_text(target, "uuid", self.resolver.path(target))
        cross_entry_path = None
        cross_entry = element.find("crossEntry")
        if cross_entry is not None:
            cross_entry_path = self.resolver.path(self.resolver.resolve(cross_entry))
        units = tuple(self._read_unit(unit) for unit in element.findall("./units/unit"))
        return RawTransaction(
            uuid=_required_text(element, "uuid", path),
            owner_kind=owner_kind,
            owner_uuid=owner_uuid,
            raw_type=_required_text(element, "type", path).upper(),
            timestamp=_datetime(_required_text(element, "date", path), f"{path}/date"),
            currency=(_optional_text(element, "currencyCode") or default_currency).upper(),
            amount=money_value(_optional_text(element, "amount") or "0"),
            shares=share_value(_optional_text(element, "shares") or "0"),
            security_uuid=security_uuid,
            note=_optional_text(element, "note") or "",
            units=units,
            cross_entry_path=cross_entry_path,
            source=SourceLocation(path, alias_path if alias_path != path else None),
        )

    def _read_unit(self, element: ET.Element) -> TransactionUnit:
        path = self.resolver.path(element)
        amount = element.find("amount")
        if amount is None:
            raise PortfolioModelError(f"transaction unit is missing amount at {path}")
        forex = element.find("forex")
        rate = _optional_text(element, "exchangeRate")
        return TransactionUnit(
            type=(element.attrib.get("type") or "UNKNOWN").upper(),
            currency=_required_attribute(amount, "currency", path).upper(),
            amount=money_value(_required_attribute(amount, "amount", path)),
            forex_currency=(forex.attrib.get("currency") or "").upper() or None
            if forex is not None
            else None,
            forex_amount=money_value(_required_attribute(forex, "amount", path))
            if forex is not None
            else None,
            exchange_rate=rate_value(rate) if rate is not None else None,
            source=SourceLocation(path),
        )

    def _read_taxonomies(self) -> tuple[Taxonomy, ...]:
        container = self.root.find("taxonomies")
        if container is None:
            return ()
        answer = []
        seen: dict[str, str] = {}
        for element in container.findall("taxonomy"):
            path = self.resolver.path(element)
            taxonomy_id = _required_text(element, "id", path)
            _insert_unique(seen, taxonomy_id, path, "taxonomy")
            root = element.find("root")
            if root is None:
                raise PortfolioModelError(f"taxonomy root is missing at {path}")
            answer.append(
                Taxonomy(
                    id=taxonomy_id,
                    name=_required_text(element, "name", path),
                    dimensions=tuple(
                        (item.text or "").strip()
                        for item in element.findall("./dimensions/string")
                        if (item.text or "").strip()
                    ),
                    root=self._read_taxonomy_node(root),
                    source=SourceLocation(path),
                )
            )
        return tuple(answer)

    def _read_taxonomy_node(self, element: ET.Element) -> TaxonomyNode:
        path = self.resolver.path(element)
        assignments = []
        for assignment in element.findall("./assignments/assignment"):
            assignment_path = self.resolver.path(assignment)
            vehicle = assignment.find("investmentVehicle")
            if vehicle is None:
                raise PortfolioModelError(f"taxonomy assignment has no vehicle at {assignment_path}")
            target = self.resolver.resolve(vehicle)
            assignments.append(
                TaxonomyAssignment(
                    investment_vehicle_type=(vehicle.attrib.get("class") or target.tag).lower(),
                    investment_vehicle_uuid=_required_text(
                        target, "uuid", self.resolver.path(target)
                    ),
                    weight=weight_value(_required_text(assignment, "weight", assignment_path)),
                    rank=_required_int(assignment, "rank", assignment_path),
                    source=SourceLocation(assignment_path),
                )
            )
        children_container = element.find("children")
        children = (
            tuple(self._read_taxonomy_node(child) for child in children_container.findall("classification"))
            if children_container is not None
            else ()
        )
        data_items = []
        for entry in element.findall("./data/entry"):
            values = [(item.text or "") for item in entry.findall("string")]
            if len(values) >= 2:
                data_items.append((values[0], values[1]))
        return TaxonomyNode(
            id=_required_text(element, "id", path),
            name=_required_text(element, "name", path),
            color=_optional_text(element, "color"),
            weight=weight_value(_optional_text(element, "weight") or "0"),
            rank=_integer(_optional_text(element, "rank") or "0", f"{path}/rank"),
            data=tuple(data_items),
            assignments=tuple(assignments),
            children=children,
            source=SourceLocation(path),
        )

    def _read_dashboards(self) -> tuple[Dashboard, ...]:
        container = self.root.find("dashboards")
        if container is None:
            return ()
        answer = []
        seen: dict[str, str] = {}
        for element in container.findall("dashboard"):
            path = self.resolver.path(element)
            dashboard_id = _required_text(element, "id", path)
            _insert_unique(seen, dashboard_id, path, "dashboard")
            answer.append(
                Dashboard(
                    id=dashboard_id,
                    name=(element.attrib.get("name") or "").strip() or dashboard_id,
                    source=SourceLocation(path),
                )
            )
        return tuple(answer)

    def _read_client_filters(
        self,
        accounts: Mapping[str, Account],
        portfolios: Mapping[str, Portfolio],
    ) -> tuple[ClientFilter, ...]:
        """Read the saved Grouped Accounts filters from Portfolio Performance settings."""
        for entry in self.root.findall("./settings/configurationSets/entry"):
            if (entry.findtext("string") or "").strip() != "client-filter-definitions":
                continue
            answer = []
            seen: set[str] = set()
            for element in entry.findall("./config-set/configurations/config"):
                path = self.resolver.path(element)
                filter_uuid = _required_text(element, "uuid", path)
                if filter_uuid in seen:
                    raise PortfolioModelError(f"duplicate client filter UUID at {path}: {filter_uuid}")
                seen.add(filter_uuid)
                members = []
                member_uuids: set[str] = set()
                for raw_token in (_optional_text(element, "data") or "").split(","):
                    token = raw_token.strip()
                    if not token:
                        continue
                    vehicle_uuid, separator, raw_weight = token.partition(":")
                    weight_raw = _integer(raw_weight, f"{path}/data") if separator else 10000
                    if not 1 <= weight_raw <= 10000:
                        raise PortfolioModelError(
                            f"client filter weight outside 1..10000 at {path}: {weight_raw}"
                        )
                    if vehicle_uuid in member_uuids:
                        raise PortfolioModelError(
                            f"duplicate client filter member at {path}: {vehicle_uuid}"
                        )
                    member_uuids.add(vehicle_uuid)
                    if vehicle_uuid in accounts:
                        vehicle_type = "account"
                    elif vehicle_uuid in portfolios:
                        vehicle_type = "portfolio"
                    else:
                        raise PortfolioModelError(
                            f"client filter member not found at {path}: {vehicle_uuid}"
                        )
                    members.append(ClientFilterMember(vehicle_type, vehicle_uuid, weight_raw))
                if members:
                    answer.append(
                        ClientFilter(
                            uuid=filter_uuid,
                            name=_optional_text(element, "name") or filter_uuid,
                            members=tuple(members),
                            source=SourceLocation(path),
                        )
                    )
            return tuple(answer)
        return ()


def _secure_load(
    raw_path: str | Path,
    *,
    allowed_root: str | Path | None,
    max_bytes: int,
) -> tuple[Path, ET.Element]:
    path = Path(raw_path)
    if path.is_symlink():
        raise XmlSecurityError(f"Portfolio XML must not be a symlink: {path.name}")
    if not path.is_file():
        raise XmlSecurityError(f"Portfolio XML is not a regular file: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise XmlSecurityError(f"cannot resolve Portfolio XML: {path}") from error
    if allowed_root is not None:
        boundary = Path(allowed_root).resolve(strict=True)
        try:
            resolved.relative_to(boundary)
        except ValueError as error:
            raise XmlSecurityError(
                f"Portfolio XML is outside the allowed input directory: {path.name}"
            ) from error
    size = resolved.stat().st_size
    if size > max_bytes:
        raise XmlSecurityError(
            f"Portfolio XML exceeds the {max_bytes}-byte safety limit: {size}"
        )
    payload = resolved.read_bytes()
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise XmlSecurityError("Portfolio XML must not contain DOCTYPE or ENTITY declarations")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise PortfolioModelError(f"malformed Portfolio XML: {error}") from error
    return resolved, root


def _insert_unique(mapping: dict, key: str, path: str, kind: str) -> None:
    if key in mapping:
        previous = mapping[key]
        previous_path = previous if isinstance(previous, str) else previous.source.path
        raise AmbiguousReferenceError(
            f"duplicate {kind} UUID {key!r} at {previous_path} and {path}"
        )
    mapping[key] = path


def _require_unique_transactions(items: Iterable[RawTransaction], kind: str) -> None:
    seen: dict[str, str] = {}
    for item in items:
        if item.uuid in seen:
            raise AmbiguousReferenceError(
                f"duplicate {kind} transaction UUID {item.uuid!r} at {seen[item.uuid]} and {item.source.path}"
            )
        seen[item.uuid] = item.source.path


def _required_text(element: ET.Element, child: str, path: str) -> str:
    value = _optional_text(element, child)
    if value is None:
        raise PortfolioModelError(f"required {child} is missing at {path}")
    return value


def _optional_text(element: ET.Element, child: str) -> str | None:
    target = element.find(child)
    if target is None:
        return None
    value = (target.text or "").strip()
    return value or None


def _required_attribute(element: ET.Element, name: str, path: str) -> str:
    value = (element.attrib.get(name) or "").strip()
    if not value:
        raise PortfolioModelError(f"required attribute {name!r} is missing at {path}")
    return value


def _required_int(element: ET.Element, child: str, path: str) -> int:
    return _integer(_required_text(element, child, path), f"{path}/{child}")


def _integer(value: int | str, label: str) -> int:
    if isinstance(value, bool):
        raise PortfolioModelError(f"{label} must be an integer")
    try:
        text = str(value)
        if not re.fullmatch(r"-?\d+", text):
            raise ValueError(text)
        return int(text)
    except (TypeError, ValueError) as error:
        raise PortfolioModelError(f"{label} must be an integer: {value!r}") from error


def _boolean(value: str, path: str) -> bool:
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise PortfolioModelError(f"boolean value is invalid at {path}: {value!r}")
    return normalized == "true"


def _date(value: str | None, label: str) -> date:
    if value is None:
        raise PortfolioModelError(f"date is missing at {label}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise PortfolioModelError(f"date is invalid at {label}: {value!r}") from error
    return parsed


def _datetime(value: str, label: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PortfolioModelError(f"datetime is invalid at {label}: {value!r}") from error


def _count_values(values: Iterable[str]) -> dict[str, int]:
    answer: dict[str, int] = {}
    for value in values:
        answer[value] = answer.get(value, 0) + 1
    return dict(sorted(answer.items()))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit a Portfolio Performance XML model")
    parser.add_argument("--audit", required=True, type=Path, metavar="XML")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        model = PortfolioModel.from_path(args.audit)
    except PortfolioModelError as error:
        sys.stderr.write(f"Portfolio XML audit failed: {error}\n")
        return 2
    sys.stdout.write(json.dumps(model.audit_summary(), ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
