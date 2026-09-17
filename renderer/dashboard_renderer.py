#!/usr/bin/env python3
"""Render an autonomous Portfolio Performance dashboard from calculated artifacts."""

from __future__ import annotations

import argparse
import base64
from bisect import bisect_left
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from renderer.detailed_report import script_safe_json, validate_detailed_report


RENDERER_VERSION = "1.7.0"
NUMERIC_TOLERANCE = Decimal("1e-12")
CHART_DOWNSAMPLE_THRESHOLD = 1200
# Public-portfolio sanitization: neutral portfolio mark replaces employer asset.
DEFAULT_MARK = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI2NCIgaGVpZ2h0PSI2NCI+PHJlY3Qgd2lkdGg9IjY0IiBoZWlnaHQ9IjY0IiByeD0iMTIiIGZpbGw9IiNlYTk5NWQiLz48dGV4dCB4PSIzMiIgeT0iNDIiIHRleHQtYW5jaG9yPSJtaWRkbGUiIGZpbGw9IndoaXRlIiBmb250LWZhbWlseT0ic2Fucy1zZXJpZiIgZm9udC1zaXplPSIyNyI+UFA8L3RleHQ+PC9zdmc+'


class DashboardError(Exception):
    """A user-facing validation or rendering error."""


@dataclass(frozen=True)
class SeriesPoint:
    day: date
    date_text: str
    cumulative_ttwror: Decimal
    portfolio_market_value: Decimal


class InlineLogoParser(HTMLParser):
    """Extract only the first inline image; template text is never consumed."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.logo: str | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if self.logo is not None or tag.lower() != "img":
            return
        source = dict(attrs).get("src")
        if source and source.startswith("data:image/") and ";base64," in source:
            self.logo = source


def read_text(path: Path, label: str) -> str:
    if not path.is_file():
        raise DashboardError(f"{label} not found: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise DashboardError(f"cannot read {label}: {path}: {error}") from error


def path_value(model: dict[str, Any], dotted_path: str) -> Any:
    value: Any = model
    for token in dotted_path.split("."):
        if not isinstance(value, dict) or token not in value:
            raise DashboardError(f"report.json is missing required field: {dotted_path}")
        value = value[token]
    if value is None:
        raise DashboardError(f"report.json required field is null: {dotted_path}")
    return value


def required_text(model: dict[str, Any], dotted_path: str) -> str:
    value = path_value(model, dotted_path)
    if not isinstance(value, str) or not value.strip():
        raise DashboardError(
            f"report.json field must be a non-empty string: {dotted_path}"
        )
    return value.strip()


def required_decimal(model: dict[str, Any], dotted_path: str) -> Decimal:
    value = path_value(model, dotted_path)
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise DashboardError(f"report.json field must be a number: {dotted_path}")
    result = value if isinstance(value, Decimal) else Decimal(value)
    if not result.is_finite():
        raise DashboardError(f"report.json field must be finite: {dotted_path}")
    return result


def decimal_value(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise DashboardError(f"{label} must be a number")
    result = value if isinstance(value, Decimal) else Decimal(value)
    if not result.is_finite():
        raise DashboardError(f"{label} must be finite")
    return result


def text_value(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DashboardError(f"{label} must be a non-empty string")
    return value.strip()


def validate_portfolio_overview(model: dict[str, Any]) -> None:
    overview = path_value(model, "portfolio_overview")
    if not isinstance(overview, dict):
        raise DashboardError("report.json portfolio_overview must be an object")
    portfolio_total = decimal_value(
        model.get("portfolio_market_value"),
        "report.json portfolio_market_value",
    )
    if portfolio_total == 0:
        raise DashboardError(
            "report.json portfolio_market_value must be nonzero when weights are present"
        )

    def reconciled_weight(
        market_value: Decimal,
        weight: Decimal,
        label: str,
    ) -> None:
        expected = market_value / portfolio_total
        if abs(weight - expected) > NUMERIC_TOLERANCE:
            raise DashboardError(
                f"{label} does not reconcile to market_value / portfolio_market_value"
            )

    cash = overview.get("cash")
    if not isinstance(cash, dict):
        raise DashboardError("report.json portfolio_overview.cash must be an object")
    cash_market_value = decimal_value(
        cash.get("market_value"),
        "report.json portfolio_overview.cash.market_value",
    )
    cash_weight = decimal_value(
        cash.get("weight"),
        "report.json portfolio_overview.cash.weight",
    )
    reconciled_weight(
        cash_market_value,
        cash_weight,
        "report.json portfolio_overview.cash.weight",
    )

    allocation = overview.get("allocation_by_asset_class")
    if not isinstance(allocation, list):
        raise DashboardError(
            "report.json portfolio_overview.allocation_by_asset_class "
            "must be an array"
        )
    allocation_weights = [cash_weight]
    for index, item in enumerate(allocation):
        label = (
            "report.json portfolio_overview.allocation_by_asset_class"
            f"[{index}]"
        )
        if not isinstance(item, dict):
            raise DashboardError(f"{label} must be an object")
        text_value(item.get("name"), f"{label}.name")
        market_value = decimal_value(item.get("market_value"), f"{label}.market_value")
        weight = decimal_value(item.get("weight"), f"{label}.weight")
        reconciled_weight(market_value, weight, f"{label}.weight")
        allocation_weights.append(
            weight
        )

    holdings = overview.get("top_holdings")
    if not isinstance(holdings, list):
        raise DashboardError(
            "report.json portfolio_overview.top_holdings must be an array"
        )
    if len(holdings) > 5:
        raise DashboardError(
            "report.json portfolio_overview.top_holdings must have at most 5 items"
        )
    previous_market_value: Decimal | None = None
    for index, item in enumerate(holdings):
        label = f"report.json portfolio_overview.top_holdings[{index}]"
        if not isinstance(item, dict):
            raise DashboardError(f"{label} must be an object")
        for field in ("security_uuid", "name", "currency", "asset_class"):
            text_value(item.get(field), f"{label}.{field}")
        market_value = decimal_value(
            item.get("market_value"), f"{label}.market_value"
        )
        weight = decimal_value(item.get("weight"), f"{label}.weight")
        reconciled_weight(market_value, weight, f"{label}.weight")
        if (
            previous_market_value is not None
            and market_value > previous_market_value
        ):
            raise DashboardError(
                "report.json portfolio_overview.top_holdings must be sorted "
                "by descending market_value"
            )
        previous_market_value = market_value

    scoped_holdings = overview.get("scoped_holdings")
    if not isinstance(scoped_holdings, list):
        raise DashboardError(
            "report.json portfolio_overview.scoped_holdings must be an array"
        )
    scoped_ids: set[str] = set()
    previous_market_value = None
    for index, item in enumerate(scoped_holdings):
        label = f"report.json portfolio_overview.scoped_holdings[{index}]"
        if not isinstance(item, dict):
            raise DashboardError(f"{label} must be an object")
        for field in ("security_uuid", "name", "currency", "asset_class"):
            text_value(item.get(field), f"{label}.{field}")
        identity = str(item["security_uuid"])
        if identity in scoped_ids:
            raise DashboardError(
                "report.json portfolio_overview.scoped_holdings contains duplicate security_uuid"
            )
        scoped_ids.add(identity)
        market_value = decimal_value(item.get("market_value"), f"{label}.market_value")
        weight = decimal_value(item.get("weight"), f"{label}.weight")
        reconciled_weight(market_value, weight, f"{label}.weight")
        if previous_market_value is not None and market_value > previous_market_value:
            raise DashboardError(
                "report.json portfolio_overview.scoped_holdings must be sorted by descending market_value"
            )
        previous_market_value = market_value
    if [item["security_uuid"] for item in holdings] != [
        item["security_uuid"] for item in scoped_holdings[:5]
    ]:
        raise DashboardError(
            "report.json portfolio_overview.top_holdings must be the leading scoped holdings"
        )

    scope = overview.get("scope")
    if not isinstance(scope, dict):
        raise DashboardError("report.json portfolio_overview.scope must be an object")
    if scope.get("mode") not in {"FULL_CLIENT", "CLIENT_FILTER"}:
        raise DashboardError("report.json portfolio_overview.scope.mode invalid")
    for field in ("account_uuids", "portfolio_uuids"):
        values = scope.get(field)
        if not isinstance(values, list) or not all(
            isinstance(item, str) and item for item in values
        ):
            raise DashboardError(f"report.json portfolio_overview.scope.{field} invalid")

    if abs(sum(allocation_weights, Decimal(0)) - Decimal(1)) > NUMERIC_TOLERANCE:
        raise DashboardError(
            "report.json portfolio_overview allocation weights do not reconcile to 1"
        )
    scoped_weight_total = cash_weight + sum(
        (
            decimal_value(
                item.get("weight"),
                f"report.json portfolio_overview.scoped_holdings[{index}].weight",
            )
            for index, item in enumerate(scoped_holdings)
        ),
        Decimal(0),
    )
    if abs(scoped_weight_total - Decimal(1)) > NUMERIC_TOLERANCE:
        raise DashboardError(
            "report.json portfolio_overview scoped holding weights do not reconcile to 1"
        )


def iso_date(value: str, label: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise DashboardError(f"{label} is not a valid ISO date: {value!r}") from error
    if parsed.isoformat() != value:
        raise DashboardError(f"{label} must use YYYY-MM-DD: {value!r}")
    return parsed


def load_report(path: Path) -> dict[str, Any]:
    raw = read_text(path, "report.json")
    try:
        model = json.loads(raw, parse_float=Decimal, parse_int=Decimal)
    except (json.JSONDecodeError, InvalidOperation) as error:
        raise DashboardError(f"report.json is invalid JSON: {error}") from error
    if not isinstance(model, dict):
        raise DashboardError("report.json root must be an object")

    required_text(model, "reporting_currency")
    iso_date(required_text(model, "report_date"), "report.json report_date")
    iso_date(
        required_text(model, "periods.all_time.effective_end_date"),
        "report.json all-time effective_end_date",
    )
    required_text(model, "calculation_engine.source")
    required_text(model, "calculation_engine.version_or_commit")

    required_decimal(model, "portfolio_market_value")
    validate_portfolio_overview(model)
    for period in ("all_time", "current_year"):
        for metric in ("cumulative_ttwror", "annualized_ttwror", "irr", "profit"):
            required_decimal(model, f"periods.{period}.{metric}")
    return model


def parse_csv_decimal(value: str | None, row_number: int, column: str) -> Decimal:
    if value is None or not value.strip():
        raise DashboardError(
            f"performance_series.csv row {row_number} has an empty {column}"
        )
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise DashboardError(
            f"performance_series.csv row {row_number} has invalid number "
            f"in {column}: {value!r}"
        ) from error
    if not number.is_finite():
        raise DashboardError(
            f"performance_series.csv row {row_number} has non-finite "
            f"{column}: {value!r}"
        )
    return number


def load_series(path: Path) -> list[SeriesPoint]:
    if not path.is_file():
        raise DashboardError(f"performance_series.csv not found: {path}")
    try:
        stream = path.open(newline="", encoding="utf-8-sig")
    except OSError as error:
        raise DashboardError(
            f"cannot read performance_series.csv: {path}: {error}"
        ) from error

    with stream:
        reader = csv.DictReader(stream)
        required = {"date", "cumulative_ttwror", "portfolio_market_value"}
        if reader.fieldnames is None:
            raise DashboardError("performance_series.csv has no header")
        missing = sorted(required - set(reader.fieldnames))
        if missing:
            raise DashboardError(
                "performance_series.csv is missing required columns: "
                + ", ".join(missing)
            )

        points: list[SeriesPoint] = []
        for row_number, row in enumerate(reader, start=2):
            date_text = (row.get("date") or "").strip()
            if not date_text:
                raise DashboardError(
                    f"performance_series.csv row {row_number} has an empty date"
                )
            day = iso_date(
                date_text, f"performance_series.csv row {row_number} date"
            )
            cumulative_ttwror = parse_csv_decimal(
                row.get("cumulative_ttwror"), row_number, "cumulative_ttwror"
            )
            portfolio_market_value = parse_csv_decimal(
                row.get("portfolio_market_value"),
                row_number,
                "portfolio_market_value",
            )
            if points and day <= points[-1].day:
                raise DashboardError(
                    "performance_series.csv dates must be strictly increasing; "
                    f"row {row_number} contains {date_text}"
                )
            points.append(
                SeriesPoint(
                    day,
                    date_text,
                    cumulative_ttwror,
                    portfolio_market_value,
                )
            )

    if not points:
        raise DashboardError("performance_series.csv contains no data rows")
    return points


def validate_series_against_report(
    points: list[SeriesPoint], report: dict[str, Any]
) -> None:
    expected_date = required_text(
        report, "periods.all_time.effective_end_date"
    )
    if points[-1].date_text != expected_date:
        raise DashboardError(
            "performance_series.csv last date does not match "
            "report.json all-time effective_end_date: "
            f"{points[-1].date_text} != {expected_date}"
        )

    expected_value = required_decimal(
        report, "periods.all_time.cumulative_ttwror"
    )
    difference = abs(points[-1].cumulative_ttwror - expected_value)
    if difference > NUMERIC_TOLERANCE:
        raise DashboardError(
            "performance_series.csv last cumulative_ttwror does not match "
            "report.json all-time cumulative_ttwror: "
            f"{points[-1].cumulative_ttwror} != {expected_value} "
            f"(difference {difference})"
        )

    expected_market_value = required_decimal(report, "portfolio_market_value")
    market_value_difference = abs(
        points[-1].portfolio_market_value - expected_market_value
    )
    if market_value_difference > NUMERIC_TOLERANCE:
        raise DashboardError(
            "performance_series.csv last portfolio_market_value does not match "
            "report.json portfolio_market_value: "
            f"{points[-1].portfolio_market_value} != {expected_market_value} "
            f"(difference {market_value_difference})"
        )


def downsample_points(
    points: list[SeriesPoint], threshold: int = CHART_DOWNSAMPLE_THRESHOLD
) -> list[SeriesPoint]:
    """Deterministically cap chart DOM size while retaining endpoints and extrema."""
    if threshold < 6:
        raise DashboardError("chart downsample threshold must be at least 6")
    if len(points) <= threshold:
        return points
    required = {
        0,
        len(points) - 1,
        min(range(len(points)), key=lambda index: points[index].cumulative_ttwror),
        max(range(len(points)), key=lambda index: points[index].cumulative_ttwror),
        min(range(len(points)), key=lambda index: points[index].portfolio_market_value),
        max(range(len(points)), key=lambda index: points[index].portfolio_market_value),
    }
    selected = set(required)
    for slot in range(threshold):
        index = round(slot * (len(points) - 1) / (threshold - 1))
        selected.add(index)
    if len(selected) > threshold:
        optional = sorted(selected - required)
        keep_optional = threshold - len(required)
        selected = required | set(optional[:keep_optional])
    elif len(selected) < threshold:
        for index in range(len(points)):
            selected.add(index)
            if len(selected) == threshold:
                break
    return [points[index] for index in sorted(selected)]


def load_inline_logo(path: Path) -> str:
    parser = InlineLogoParser()
    try:
        parser.feed(read_text(path, "HTML design template"))
    except Exception as error:
        if isinstance(error, DashboardError):
            raise
        raise DashboardError(f"HTML design template cannot be parsed: {error}") from error
    if parser.logo is None:
        raise DashboardError(
            "HTML design template does not contain an inline base64 logo"
        )

    encoded = parser.logo.split(",", 1)[1]
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise DashboardError("HTML design template logo has invalid base64") from error
    if not decoded:
        raise DashboardError("HTML design template logo is empty")
    return parser.logo


def format_integer(value: Decimal) -> str:
    rounded = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    sign = "-" if rounded < 0 else ""
    digits = f"{abs(int(rounded)):,}".replace(",", " ")
    return sign + digits


def format_money(value: Decimal, currency: str) -> str:
    amount = format_integer(abs(value))
    sign = "-" if value < 0 else ""
    symbols = {"USD": "$", "EUR": "€", "GBP": "£"}
    if currency in symbols:
        return f"{sign}{symbols[currency]}{amount}"
    return f"{sign}{currency} {amount}"


def format_percent(value: Decimal) -> str:
    percentage = (value * Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return f"{percentage:.2f}".replace(".", ",") + "%"


def css_percent(value: Decimal) -> str:
    percentage = (value * Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return f"{percentage:f}".rstrip("0").rstrip(".") or "0"


def css_bar_percent(value: Decimal) -> str:
    """Return a safe visual magnitude while the adjacent copy keeps the signed value."""

    return css_percent(min(abs(value), Decimal(1)))


def metric_label(label: str, explanation: str) -> str:
    return (
        '<span class="metric-label">'
        f"{escape(label)}"
        '<button class="metric-help" type="button" '
        f'aria-label="{escape(explanation, quote=True)}" '
        f'data-tooltip="{escape(explanation, quote=True)}" '
        f'title="{escape(explanation, quote=True)}">i</button>'
        "</span>"
    )


def display_date(value: str) -> str:
    parsed = iso_date(value, "display date")
    return parsed.strftime("%d.%m.%Y")


def nice_step(span: float, target_ticks: int = 7) -> float:
    raw = span / max(target_ticks, 1)
    if raw <= 0:
        return 0.01
    exponent = math.floor(math.log10(raw))
    magnitude = 10**exponent
    fraction = raw / magnitude
    if fraction <= 1:
        nice_fraction = 1
    elif fraction <= 2:
        nice_fraction = 2
    elif fraction <= 2.5:
        nice_fraction = 2.5
    elif fraction <= 5:
        nice_fraction = 5
    else:
        nice_fraction = 10
    return nice_fraction * magnitude


def quarter_start(day: date) -> date:
    month = ((day.month - 1) // 3) * 3 + 1
    return date(day.year, month, 1)


def next_quarter(day: date) -> date:
    start = quarter_start(day)
    if start.month == 10:
        return date(start.year + 1, 1, 1)
    return date(start.year, start.month + 3, 1)


@dataclass(frozen=True)
class QuarterBand:
    quarter: date
    visible_start: date
    visible_end: date
    label: str
    partial: str


def quarter_bands(points: list[SeriesPoint]) -> list[QuarterBand]:
    """Return exact calendar-quarter intersections with the visible interval."""

    first_day = points[0].day
    last_day = points[-1].day
    quarter = quarter_start(first_day)
    bands = []
    while quarter <= last_day:
        following = next_quarter(quarter)
        visible_start = max(first_day, quarter)
        visible_end = min(last_day, following)
        if visible_end >= visible_start:
            partial_start = visible_start > quarter
            partial_end = last_day < following - timedelta(days=1)
            partial = (
                "both"
                if partial_start and partial_end
                else "start"
                if partial_start
                else "end"
                if partial_end
                else "none"
            )
            quarter_number = ((quarter.month - 1) // 3) + 1
            bands.append(
                QuarterBand(
                    quarter,
                    visible_start,
                    visible_end,
                    f"Q{quarter_number} {quarter.year}",
                    partial,
                )
            )
        quarter = following
    return bands


def quarter_ticks(
    points: list[SeriesPoint], *, mobile: bool
) -> list[tuple[date, date, str]]:
    dates = [point.day for point in points]
    ticks = [
        (
            band.quarter,
            dates[bisect_left(dates, band.visible_start)],
            band.label,
        )
        for band in quarter_bands(points)
    ]

    if mobile and len(ticks) > 4:
        selected = list(range(0, len(ticks), 2))
        last_index = len(ticks) - 1
        if last_index not in selected:
            selected.append(last_index)
        ticks = [ticks[index] for index in selected]

    return ticks


def chart_y_domain(
    point_groups: list[list[SeriesPoint]], *, metric: str, mobile: bool
) -> tuple[float, float, float]:
    if metric not in {"return", "market"}:
        raise DashboardError(f"unsupported chart metric: {metric}")
    values = [
        float(
            point.cumulative_ttwror
            if metric == "return"
            else point.portfolio_market_value
        )
        for points in point_groups
        for point in points
    ]
    if not values:
        raise DashboardError("chart y-domain requires at least one point")
    raw_min = min(min(values), 0.0)
    raw_max = max(max(values), 0.0)
    span = raw_max - raw_min
    if span == 0:
        span = 0.02 if metric == "return" else max(abs(raw_max), 1.0)
        raw_max += span
    padding = span * 0.06
    step = nice_step(
        (raw_max + padding) - (raw_min - padding),
        target_ticks=4 if mobile else 6,
    )
    y_min = (
        0.0
        if metric == "market"
        else min(0.0, math.floor((raw_min - padding) / step) * step)
    )
    y_max = math.ceil((raw_max + padding) / step) * step
    if y_max <= y_min:
        y_max = y_min + step
    return y_min, y_max, step


def format_axis_money(value: float, currency: str) -> str:
    symbols = {"USD": "$", "EUR": "€", "GBP": "£"}
    prefix = symbols.get(currency, f"{currency} ")
    absolute = abs(value)
    sign = "−" if value < 0 else ""
    if absolute >= 1_000_000:
        amount = f"{absolute / 1_000_000:.1f}".replace(".", ",")
        amount = amount.removesuffix(",0")
        return f"{sign}{prefix}{amount} млн"
    if absolute >= 1_000:
        amount = f"{absolute / 1_000:.0f}"
        return f"{sign}{prefix}{amount} тыс."
    return f"{sign}{prefix}{absolute:.0f}"


def render_chart(
    points: list[SeriesPoint],
    *,
    metric: str,
    currency: str,
    mobile: bool = False,
    period: str = "all_time",
    focusable: bool = True,
    y_domain: tuple[float, float, float] | None = None,
) -> str:
    if metric not in {"return", "market"}:
        raise DashboardError(f"unsupported chart metric: {metric}")

    if mobile:
        width = 390.0
        height = 285.0
        left = 12.0
        right = 340.0
        top = 16.0
        bottom = 232.0
        chart_variant = "mobile"
    else:
        width = 760.0
        height = 330.0
        left = 22.0
        right = 690.0
        top = 18.0
        bottom = 274.0
        chart_variant = "desktop"
    plot_width = right - left
    plot_height = bottom - top

    if metric == "return":
        source_field = "cumulative_ttwror"
        decimal_values = [point.cumulative_ttwror for point in points]
        display_value = format_percent
        aria_metric = "накопленная доходность"
    else:
        source_field = "portfolio_market_value"
        decimal_values = [point.portfolio_market_value for point in points]
        display_value = lambda value: format_money(value, currency)
        aria_metric = "рыночная стоимость портфеля"

    y_min, y_max, step = y_domain or chart_y_domain(
        [points], metric=metric, mobile=mobile
    )

    first_day = points[0].day
    last_day = points[-1].day
    day_span = max((last_day - first_day).days, 1)

    def x(day: date) -> float:
        return left + ((day - first_day).days / day_span) * plot_width

    def y(value: float) -> float:
        return bottom - ((value - y_min) / (y_max - y_min)) * plot_height

    coords = [
        (x(point.day), y(float(value)))
        for point, value in zip(points, decimal_values)
    ]
    line_path = " ".join(
        ("M" if index == 0 else "L") + f" {px:.2f} {py:.2f}"
        for index, (px, py) in enumerate(coords)
    )
    baseline_y = y(0.0)
    area_path = (
        line_path
        + f" L {coords[-1][0]:.2f} {baseline_y:.2f}"
        + f" L {coords[0][0]:.2f} {baseline_y:.2f} Z"
    )

    tick_lines: list[str] = []
    tick_count = int(round((y_max - y_min) / step))
    percent_decimals = 1 if step * 100 < 1 else 0
    for index in range(tick_count + 1):
        value = y_min + index * step
        py = y(value)
        css_class = (
            "zero-line"
            if metric == "return" and abs(value) < step / 100
            else "grid"
        )
        if metric == "return":
            label = (
                f"{value * 100:.{percent_decimals}f}".replace(".", ",") + "%"
            )
        else:
            label = format_axis_money(value, currency)
        tick_lines.append(
            f'<line class="{css_class}" x1="{left:.2f}" y1="{py:.2f}" '
            f'x2="{right:.2f}" y2="{py:.2f}"/>'
        )
        tick_lines.append(
            f'<text class="axis-text" x="{right + 9:.2f}" '
            f'y="{py + 3:.2f}">{escape(label)}</text>'
        )

    bands = quarter_bands(points)
    visible_label_indexes = set(range(len(bands)))
    if mobile and len(bands) > 4:
        visible_label_indexes = set(range(0, len(bands), 2))
        visible_label_indexes.add(len(bands) - 1)
    quarter_tick_markup: list[str] = []
    for index, band in enumerate(bands):
        px = x(band.visible_start)
        next_px = x(band.visible_end)
        label_x = px + ((next_px - px) / 2)
        text_anchor = "middle"
        label_placement = "quarter-band"
        quarter_tick_markup.append(
            '<g class="quarter-segment" role="img" '
            f'aria-label="{escape(band.label, quote=True)}; '
            f'{"полный квартал" if band.partial == "none" else "неполный квартал"}" '
            f'data-quarter-partial="{band.partial}" '
            f'data-visible-start="{band.visible_start.isoformat()}" '
            f'data-visible-end="{band.visible_end.isoformat()}">'
            f'<rect class="quarter-band quarter-band-{index % 2}" '
            f'fill="{("#fffaf7" if index % 2 == 0 else "#ffffff")}" '
            f'x="{px:.2f}" y="{top:.2f}" '
            f'width="{max(next_px - px, 0):.2f}" '
            f'height="{plot_height:.2f}" '
            f'data-quarter-span="{escape(band.label, quote=True)}"/>'
        )
        quarter_tick_markup.append(
            f'<line class="quarter-grid" x1="{px:.2f}" y1="{top:.2f}" '
            f'x2="{px:.2f}" y2="{bottom:.2f}" '
            f'data-quarter-start="{band.quarter.isoformat()}" '
            f'data-tick-date="{band.visible_start.isoformat()}"/>'
        )
        if index in visible_label_indexes:
            quarter_tick_markup.append(
                f'<text class="quarter-text" x="{label_x:.2f}" '
                f'y="{bottom + 28:.2f}" text-anchor="{text_anchor}" '
                f'data-grid-x="{px:.2f}" data-band-right="{next_px:.2f}" '
                f'data-label-placement="{label_placement}" '
                f'data-quarter="{escape(band.label, quote=True)}">{escape(band.label)}</text>'
            )
        quarter_tick_markup.append("</g>")

    tooltip_width = 160.0
    tooltip_height = 44.0
    hit_items: list[str] = []
    fallback_items: list[str] = []
    for index, (point, value, (px, py)) in enumerate(
        zip(points, decimal_values, coords)
    ):
        hit_left = (
            left
            if index == 0
            else (coords[index - 1][0] + px) / 2
        )
        hit_right = (
            right
            if index + 1 == len(coords)
            else (px + coords[index + 1][0]) / 2
        )
        display_day = display_date(point.date_text)
        display_point_value = display_value(value)
        hit_items.append(
            f'<rect class="chart-hit-point" fill="none" stroke="none" x="{hit_left:.2f}" '
            f'y="{top:.2f}" width="{max(hit_right - hit_left, 0.1):.2f}" '
            f'height="{plot_height:.2f}" data-point-index="{index}" '
            f'data-point-x="{px:.2f}" data-point-y="{py:.2f}" '
            f'data-date="{escape(point.date_text, quote=True)}" '
            f'data-display-date="{display_day}" '
            f'data-display-value="{escape(display_point_value, quote=True)}"/>'
        )
        if mobile:
            preferred_x = (
                px + 12
                if px + tooltip_width + 12 <= right
                else px - tooltip_width - 12
            )
            preferred_y = (
                py - tooltip_height - 12
                if py - tooltip_height - 12 >= top
                else py + 12
            )
            tooltip_x = max(
                left + 4,
                min(preferred_x, right - tooltip_width - 4),
            )
            tooltip_y = max(
                top + 4,
                min(preferred_y, bottom - tooltip_height - 4),
            )
            fallback_items.append(
                '<g class="chart-fallback-item">'
                f'<foreignObject x="{hit_left:.2f}" y="{top:.2f}" '
                f'width="{max(hit_right - hit_left, 0.1):.2f}" '
                f'height="{plot_height:.2f}">'
                '<button '
                'class="chart-fallback-button" type="button" '
                f'aria-label="{display_day}: '
                f'{escape(display_point_value, quote=True)}"></button>'
                "</foreignObject>"
                '<g class="chart-fallback-tooltip">'
                f'<line class="tooltip-guide" x1="{px:.2f}" '
                f'y1="{top:.2f}" x2="{px:.2f}" y2="{bottom:.2f}"/>'
                f'<circle class="tooltip-marker" cx="{px:.2f}" '
                f'cy="{py:.2f}" r="4"/>'
                f'<g class="tooltip-box" '
                f'transform="translate({tooltip_x:.2f},{tooltip_y:.2f})">'
                f'<rect width="{tooltip_width:.0f}" '
                f'height="{tooltip_height:.0f}" rx="8"/>'
                f'<text class="tooltip-date" x="11" y="17">'
                f"{display_day}</text>"
                f'<text class="tooltip-value" x="11" y="34">'
                f"{escape(display_point_value)}</text>"
                "</g></g></g>"
            )
    fallback_markup = (
        f'\n          <g class="chart-fallback-layer">'
        f'{"".join(fallback_items)}</g>'
        if mobile
        else ""
    )
    start_x, start_y = coords[0]
    end_x, end_y = coords[-1]
    last_label = display_value(decimal_values[-1])
    aria_label = (
        f"График: {aria_metric}; {points[0].date_text} — "
        f"{points[-1].date_text}; последняя точка {last_label}"
    )
    chart_id = f"{metric}-{period}-{chart_variant}"
    return f"""
      <svg class="performance-chart performance-chart-{chart_variant} chart-mode-{metric}"
           viewBox="0 0 {width:.0f} {height:.0f}"
           role="group" tabindex="{0 if focusable else -1}" aria-label="{escape(aria_label, quote=True)}"
           data-chart-metric="{metric}"
           data-chart-currency="{escape(currency, quote=True)}"
           data-chart-period="{escape(period, quote=True)}"
           data-source-field="{source_field}"
           data-chart-variant="{chart_variant}"
           data-y-axis-count="1"
           data-point-count="{len(points)}"
           data-first-date="{points[0].date_text}"
           data-last-date="{points[-1].date_text}"
           data-last-value="{decimal_values[-1]}"
           data-last-cumulative-ttwror="{points[-1].cumulative_ttwror}"
           data-last-portfolio-market-value="{points[-1].portfolio_market_value}"
           data-x-scale="calendar-days"
           data-day-span="{day_span}"
           data-plot-left="{left:.2f}"
           data-plot-right="{right:.2f}"
           data-plot-top="{top:.2f}"
           data-plot-bottom="{bottom:.2f}"
           data-y-min="{y_min:.12g}"
           data-y-max="{y_max:.12g}">
        <defs>
          <linearGradient id="areaGradient-{chart_id}" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stop-color="#FF914C" stop-opacity=".10"/>
            <stop offset="100%" stop-color="#FF914C" stop-opacity=".01"/>
          </linearGradient>
        </defs>
        <g>
          {"".join(tick_lines)}
          {"".join(quarter_tick_markup)}
          <path class="performance-area"
                fill="url(#areaGradient-{chart_id})" d="{area_path}"/>
          <path class="performance-line" d="{line_path}" fill="none" stroke="#FF914C"/>
          <circle class="endpoint endpoint-start" cx="{start_x:.2f}" cy="{start_y:.2f}" r="2.5"/>
          <circle class="endpoint endpoint-end" cx="{end_x:.2f}" cy="{end_y:.2f}" r="3.5"/>
          <g class="chart-hit-layer">{"".join(hit_items)}</g>
          <g class="chart-live-tooltip" hidden
             data-tooltip-width="{tooltip_width:.0f}"
             data-tooltip-height="{tooltip_height:.0f}">
            <line class="tooltip-guide" x1="0" y1="{top:.2f}"
                  x2="0" y2="{bottom:.2f}"/>
            <circle class="tooltip-marker" cx="0" cy="0" r="4"/>
            <g class="tooltip-box" transform="translate(0,0)">
              <rect width="{tooltip_width:.0f}"
                    height="{tooltip_height:.0f}" rx="8" fill="#ffffff"/>
              <text class="tooltip-date" x="11" y="17"></text>
              <text class="tooltip-value" x="11" y="34"></text>
            </g>
          </g>{fallback_markup}
        </g>
      </svg>"""


def metric_cell(
    element_id: str, source_field: str, display_value: str, css_class: str = ""
) -> str:
    classes = "metric-value" + (f" {css_class}" if css_class else "")
    return (
        f'<td id="{element_id}" class="{classes}" '
        f'data-source-field="{source_field}">{escape(display_value)}</td>'
    )


def render_dashboard(
    report: dict[str, Any],
    points: list[SeriesPoint],
    logo: str,
    report_source: Path,
    series_source: Path,
) -> str:
    currency = required_text(report, "reporting_currency").upper()
    report_date = required_text(report, "report_date")
    engine = required_text(report, "calculation_engine.source")
    commit = required_text(report, "calculation_engine.version_or_commit")

    all_time = {
        metric: required_decimal(report, f"periods.all_time.{metric}")
        for metric in ("cumulative_ttwror", "annualized_ttwror", "irr", "profit")
    }
    current_year = {
        metric: required_decimal(report, f"periods.current_year.{metric}")
        for metric in ("cumulative_ttwror", "annualized_ttwror", "irr", "profit")
    }
    market_value = required_decimal(report, "portfolio_market_value")
    return_desktop_chart = render_chart(
        points, metric="return", currency=currency
    )
    return_mobile_chart = render_chart(
        points, metric="return", currency=currency, mobile=True
    )
    market_desktop_chart = render_chart(
        points, metric="market", currency=currency
    )
    market_mobile_chart = render_chart(
        points, metric="market", currency=currency, mobile=True
    )
    last_performance_display = format_percent(points[-1].cumulative_ttwror)
    last_market_display = format_money(
        points[-1].portfolio_market_value, currency
    )
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    chart_data = json.dumps(
        [
            {
                "date": point.date_text,
                "cumulative_ttwror": str(point.cumulative_ttwror),
                "portfolio_market_value": str(point.portfolio_market_value),
            }
            for point in points
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")

    explanations = {
        "cumulative": "Доходность без влияния пополнений и выводов средств",
        "annualized": "Доходность периода, приведённая к годовому выражению",
        "irr": "Доходность с учётом времени денежных потоков",
        "profit": "Абсолютный финансовый результат периода",
    }
    rows = [
        (
            metric_label("Совокупная доходность", explanations["cumulative"]),
            metric_cell(
                "metric-all-time-cumulative",
                "periods.all_time.cumulative_ttwror",
                format_percent(all_time["cumulative_ttwror"]),
            ),
            metric_cell(
                "metric-current-year-cumulative",
                "periods.current_year.cumulative_ttwror",
                format_percent(current_year["cumulative_ttwror"]),
            ),
        ),
        (
            metric_label(
                "Доходность в годовом выражении",
                explanations["annualized"],
            ),
            metric_cell(
                "metric-all-time-annualized",
                "periods.all_time.annualized_ttwror",
                format_percent(all_time["annualized_ttwror"]),
            ),
            metric_cell(
                "metric-current-year-annualized",
                "periods.current_year.annualized_ttwror",
                format_percent(current_year["annualized_ttwror"]),
            ),
        ),
        (
            metric_label("IRR", explanations["irr"]),
            metric_cell(
                "metric-all-time-irr",
                "periods.all_time.irr",
                format_percent(all_time["irr"]),
            ),
            metric_cell(
                "metric-current-year-irr",
                "periods.current_year.irr",
                format_percent(current_year["irr"]),
            ),
        ),
        (
            metric_label("Прибыль", explanations["profit"]),
            metric_cell(
                "metric-all-time-profit",
                "periods.all_time.profit",
                format_money(all_time["profit"], currency),
                "money",
            ),
            metric_cell(
                "metric-current-year-profit",
                "periods.current_year.profit",
                format_money(current_year["profit"], currency),
                "money",
            ),
        ),
    ]
    table_rows = "\n".join(
        f"<tr><th scope=\"row\" class=\"stub\">{label}</th>{left}{right}</tr>"
        for label, left, right in rows
    )
    report_display_date = display_date(report_date)
    market_display = format_money(market_value, currency)
    mobile_metric_definitions = [
        (
            "cumulative",
            "Совокупная доходность",
            explanations["cumulative"],
            format_percent(all_time["cumulative_ttwror"]),
            format_percent(current_year["cumulative_ttwror"]),
        ),
        (
            "annualized",
            "Доходность в годовом выражении",
            explanations["annualized"],
            format_percent(all_time["annualized_ttwror"]),
            format_percent(current_year["annualized_ttwror"]),
        ),
        (
            "irr",
            "IRR",
            explanations["irr"],
            format_percent(all_time["irr"]),
            format_percent(current_year["irr"]),
        ),
        (
            "profit",
            "Прибыль",
            explanations["profit"],
            format_money(all_time["profit"], currency),
            format_money(current_year["profit"], currency),
        ),
    ]

    def mobile_period(period: str, title: str, value_index: int) -> str:
        metric_rows_list: list[str] = []
        for key, label, explanation, *values in mobile_metric_definitions:
            metric_rows_list.append(
                f'<details class="mobile-metric-row" '
                f'data-mobile-metric="{period}.{key}">'
                '<summary class="mobile-metric-summary">'
                '<span class="mobile-metric-label">'
                f"{escape(label)}"
                '<span class="mobile-metric-info" aria-hidden="true">i</span>'
                "</span>"
                f'<strong id="mobile-{period}-{key}">'
                f"{escape(values[value_index])}</strong></summary>"
                f'<p class="mobile-metric-explanation">'
                f"{escape(explanation)}</p>"
                f"</details>"
            )
        metric_rows = "\n".join(metric_rows_list)
        return (
            f'<section class="mobile-period" aria-label="{escape(title, quote=True)}">'
            f'<h3 class="mobile-period-title">{escape(title)}</h3>'
            f'<div class="mobile-period-body">{metric_rows}</div>'
            f"</section>"
        )

    mobile_metrics = (
        mobile_period("all-time", "За всё время", 0)
        + mobile_period("current-year", "За текущий год", 1)
    )

    overview = path_value(report, "portfolio_overview")
    cash = overview["cash"]
    allocation = overview["allocation_by_asset_class"]
    holdings = overview["top_holdings"]
    cash_market_value = decimal_value(
        cash["market_value"], "portfolio_overview.cash.market_value"
    )
    cash_weight = decimal_value(cash["weight"], "portfolio_overview.cash.weight")

    allocation_rows = "\n".join(
        (
            '<div class="allocation-row" '
            f'data-allocation-index="{index}" '
            f'data-source-field="portfolio_overview.allocation_by_asset_class[{index}]">'
            '<div class="allocation-copy">'
            f'<strong>{escape(text_value(item["name"], "allocation name"))}</strong>'
            "<span>"
            f'{escape(format_percent(decimal_value(item["weight"], "allocation weight")))}'
            " · "
            f'{escape(format_money(decimal_value(item["market_value"], "allocation market value"), currency))}'
            "</span></div>"
            '<div class="allocation-track" aria-hidden="true">'
            f'<span style="width:{css_bar_percent(decimal_value(item["weight"], "allocation weight"))}%"></span>'
            "</div></div>"
        )
        for index, item in enumerate(allocation)
    )
    cash_allocation_row = (
        '<div class="allocation-row allocation-row-cash" '
        'data-source-field="portfolio_overview.cash">'
        '<div class="allocation-copy">'
        "<strong>Денежные средства</strong>"
        '<span class="allocation-values">'
        '<span id="overview-cash-weight">'
        f"{escape(format_percent(cash_weight))}</span>"
        " · "
        '<span id="overview-cash-value">'
        f"{escape(format_money(cash_market_value, currency))}</span>"
        "</span></div>"
        '<div class="allocation-track" aria-hidden="true">'
        f'<span style="width:{css_bar_percent(cash_weight)}%"></span>'
        "</div></div>"
    )
    allocation_rows = (
        f"{allocation_rows}\n{cash_allocation_row}"
        if allocation_rows
        else cash_allocation_row
    )

    holding_rows = "\n".join(
        (
            '<div class="holding-row" role="row" '
            f'data-security-uuid="{escape(text_value(item["security_uuid"], "holding uuid"), quote=True)}" '
            f'data-source-field="portfolio_overview.top_holdings[{index}]">'
            '<div class="holding-asset" role="cell">'
            f'<strong>{escape(text_value(item["name"], "holding name"))}</strong>'
            "<span>"
            f'{escape(text_value(item["asset_class"], "holding asset class"))}'
            " · "
            f'{escape(text_value(item["currency"], "holding currency"))}'
            "</span></div>"
            '<div class="holding-weight" role="cell" data-label="Доля">'
            f'{escape(format_percent(decimal_value(item["weight"], "holding weight")))}'
            "</div>"
            '<div class="holding-value" role="cell" data-label="Стоимость">'
            f'{escape(format_money(decimal_value(item["market_value"], "holding market value"), currency))}'
            "</div></div>"
        )
        for index, item in enumerate(holdings)
    )
    if not holding_rows:
        holding_rows = '<p class="empty-overview">Нет позиций</p>'

    return f"""<!DOCTYPE html>
<html lang="ru" class="no-js">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <meta name="report-date" content="{escape(report_date, quote=True)}"/>
  <meta name="reporting-currency" content="{escape(currency, quote=True)}"/>
  <meta name="calculation-engine" content="{escape(engine, quote=True)}"/>
  <meta name="calculation-engine-commit" content="{escape(commit, quote=True)}"/>
  <meta name="renderer-version" content="{RENDERER_VERSION}"/>
  <title>Ключевые показатели портфеля</title>
  <!-- Generated {generated_at}; renderer {RENDERER_VERSION};
       sources: {escape(report_source.name)}, {escape(series_source.name)} -->
  <style>
    :root {{
      --orange:#ff914c;
      --orange-dark:#e86f2d;
      --orange-light:#ffdfca;
      --gray-light:#e2e6e9;
      --text:#111;
      --muted:#666;
      --grid:rgba(0,0,0,.10);
      --grid-strong:rgba(0,0,0,.18);
    }}
    * {{ box-sizing:border-box; }}
    body {{
      margin:0;
      min-width:320px;
      overflow-x:hidden;
      background:linear-gradient(180deg,#fffefe 0%,#fff8f3 100%);
      color:var(--text);
      font-family:"Montserrat","Avenir Next",Avenir,Arial,sans-serif;
      -webkit-text-size-adjust:100%;
      text-size-adjust:100%;
    }}
    .dashboard {{
      width:min(calc(100vw - 32px),860px);
      margin:0 auto;
      padding:22px 0 30px;
    }}
    .sheet {{
      background:#fff;
      border:1px solid var(--gray-light);
      border-radius:24px;
      box-shadow:0 12px 38px rgba(59,37,22,.08);
      padding:24px 26px 26px;
    }}
    .hero {{
      display:flex;
      align-items:center;
      gap:16px;
      margin-bottom:18px;
      padding:4px 2px;
    }}
    .hero img {{
      width:68px;
      height:68px;
      display:block;
      flex:0 0 auto;
      object-fit:contain;
    }}
    .hero-copy {{ min-width:0; }}
    .hero-title {{
      margin:0;
      font-size:clamp(22px,3vw,30px);
      line-height:1.02;
      font-weight:700;
      letter-spacing:-.025em;
    }}
    .hero-subtitle {{
      margin-top:7px;
      color:var(--muted);
      font-size:13px;
      line-height:1.3;
    }}
    .portfolio-summary {{
      display:flex;
      align-items:flex-end;
      justify-content:space-between;
      gap:16px;
      margin:2px 2px 22px;
      padding:17px 19px;
      border:1px solid var(--orange-light);
      border-radius:16px;
      background:linear-gradient(135deg,rgba(255,223,202,.48),rgba(255,255,255,.96));
    }}
    .summary-label {{
      margin:0 0 5px;
      color:#76543e;
      font-size:12px;
      font-weight:600;
      letter-spacing:.015em;
    }}
    .summary-value {{
      font-size:clamp(26px,4.5vw,40px);
      line-height:1;
      font-weight:700;
      letter-spacing:-.035em;
      white-space:nowrap;
    }}
    .summary-context {{
      color:var(--muted);
      font-size:12px;
      line-height:1.45;
      text-align:right;
      white-space:nowrap;
    }}
    .card {{ margin-bottom:18px; }}
    [hidden] {{ display:none !important; }}
    .chart-mode-input {{
      position:absolute;
      width:1px;
      height:1px;
      overflow:hidden;
      clip:rect(0 0 0 0);
      clip-path:inset(50%);
      white-space:nowrap;
    }}
    .chart-head {{ padding:0 4px 12px; }}
    .chart-heading-row {{
      display:flex;
      align-items:flex-start;
      justify-content:space-between;
      gap:20px;
    }}
    .chart-copy {{ min-width:0; }}
    .chart-copy-market,
    .chart-current-market,
    .chart-mobile-summary-market {{ display:none; }}
    #chart-mode-market:checked ~ .chart-head .chart-copy-return,
    #chart-mode-market:checked ~ .chart-head .chart-current-return,
    #chart-mode-market:checked ~ .chart-head .chart-mobile-summary-return {{
      display:none;
    }}
    #chart-mode-market:checked ~ .chart-head .chart-copy-market {{
      display:block;
    }}
    #chart-mode-market:checked ~ .chart-head .chart-current-market {{
      display:block;
    }}
    .chart-title {{
      margin:0;
      font-size:18px;
      line-height:1.2;
      font-weight:700;
      text-align:left;
    }}
    .chart-subtitle {{
      margin:5px 0 0;
      color:var(--muted);
      font-size:12px;
      line-height:1.35;
    }}
    .chart-current {{
      flex:0 0 auto;
      min-width:152px;
      text-align:right;
    }}
    .chart-current span {{
      display:block;
      color:var(--muted);
      font-size:11px;
      line-height:1.2;
    }}
    .chart-current strong {{
      display:block;
      margin-top:3px;
      color:var(--orange-dark);
      font-size:20px;
      line-height:1;
      white-space:nowrap;
    }}
    .chart-mode-switch {{
      display:inline-flex;
      flex-wrap:nowrap;
      width:auto;
      margin-top:12px;
      padding:3px;
      border:1px solid var(--gray-light);
      border-radius:10px;
      background:#f6f6f6;
    }}
    .chart-mode-button {{
      display:grid;
      place-items:center;
      min-width:128px;
      padding:7px 12px;
      border:0;
      border-radius:7px;
      background:transparent;
      color:#575757;
      font:inherit;
      font-size:12px;
      font-weight:600;
      line-height:1.15;
      cursor:pointer;
      white-space:nowrap;
      -webkit-tap-highlight-color:transparent;
      user-select:none;
    }}
    #chart-mode-return:checked ~ .chart-head
      .chart-mode-button[for="chart-mode-return"],
    #chart-mode-market:checked ~ .chart-head
      .chart-mode-button[for="chart-mode-market"] {{
      background:#fff;
      color:#7a4423;
      box-shadow:0 1px 4px rgba(0,0,0,.12);
    }}
    #chart-mode-return:focus-visible ~ .chart-head
      .chart-mode-button[for="chart-mode-return"],
    #chart-mode-market:focus-visible ~ .chart-head
      .chart-mode-button[for="chart-mode-market"] {{
      outline:2px solid var(--orange);
      outline-offset:2px;
    }}
    .chart-shell {{
      width:100%;
      overflow:hidden;
      border:1px solid var(--gray-light);
      border-radius:14px;
      background:linear-gradient(180deg,rgba(255,223,202,.16) 0%,#fff 38%);
    }}
    .performance-chart {{
      display:none;
      width:100%;
      height:auto;
      touch-action:pan-y;
      -webkit-user-select:none;
      user-select:none;
      -webkit-touch-callout:none;
      font-family:"Montserrat","Avenir Next",Avenir,Arial,sans-serif;
    }}
    .performance-chart * {{
      -webkit-user-select:none;
      user-select:none;
      -webkit-touch-callout:none;
    }}
    .chart-mobile-summary {{
      display:none;
      align-items:center;
      justify-content:space-between;
      gap:12px;
      margin-top:10px;
      padding:9px 11px;
      border:1px solid var(--orange-light);
      border-radius:11px;
      background:#fffaf7;
      color:#76543e;
      font-size:12px;
    }}
    .chart-mobile-summary strong {{
      color:var(--text);
      font-size:14px;
      white-space:nowrap;
    }}
    .chart-no-js-note {{ display:none; }}
    .grid {{
      stroke:rgba(0,0,0,.075);
      stroke-dasharray:2 5;
      shape-rendering:crispEdges;
    }}
    .quarter-grid {{
      stroke:rgba(0,0,0,.055);
      stroke-dasharray:2 6;
      shape-rendering:crispEdges;
    }}
    .quarter-band {{
      pointer-events:none;
      shape-rendering:crispEdges;
    }}
    .quarter-band-0 {{ fill:rgba(255,145,76,.022); }}
    .quarter-band-1 {{ fill:rgba(0,0,0,.008); }}
    .zero-line {{
      stroke:rgba(70,70,70,.58);
      stroke-width:1.25;
      shape-rendering:crispEdges;
    }}
    .axis-text {{ fill:#757575; font-size:11px; }}
    .quarter-text {{
      fill:#6f6f6f;
      font-size:11px;
      font-weight:600;
    }}
    .performance-area {{ pointer-events:none; }}
    .performance-line {{
      fill:none;
      stroke:var(--orange);
      stroke-width:2.15;
      stroke-linecap:round;
      stroke-linejoin:miter;
      shape-rendering:geometricPrecision;
      pointer-events:none;
    }}
    .endpoint {{
      fill:#fff;
      stroke:var(--orange);
      stroke-width:2;
      pointer-events:none;
    }}
    .chart-hit-point {{
      fill:transparent;
      stroke:none;
      cursor:crosshair;
      outline:none;
      pointer-events:all;
    }}
    .chart-live-tooltip {{
      pointer-events:none;
    }}
    .chart-fallback-layer {{ display:none; }}
    .chart-fallback-button {{
      width:100%;
      height:100%;
      margin:0;
      padding:0;
      border:0;
      outline:0;
      background:transparent;
      cursor:crosshair;
      -webkit-appearance:none;
      appearance:none;
      -webkit-tap-highlight-color:transparent;
    }}
    .chart-fallback-tooltip {{
      visibility:hidden;
      opacity:0;
      pointer-events:none;
    }}
    .tooltip-box rect {{
      fill:rgba(255,255,255,.97);
      stroke:rgba(232,111,45,.24);
      filter:drop-shadow(0 2px 5px rgba(0,0,0,.12));
    }}
    .tooltip-box text {{
      fill:#5c5c5c;
      font-size:11px;
    }}
    .tooltip-box .tooltip-value {{
      fill:#6f4b35;
      font-weight:700;
    }}
    .tooltip-guide {{
      stroke:rgba(232,111,45,.38);
      stroke-width:1;
      stroke-dasharray:3 4;
    }}
    .tooltip-marker {{
      fill:#fff;
      stroke:var(--orange);
      stroke-width:2;
    }}
    .section-title {{
      margin:0 0 13px;
      font-size:17px;
      line-height:1.2;
      font-weight:700;
    }}
    .overview-panel {{
      padding:17px 18px;
      border:1px solid var(--gray-light);
      border-radius:16px;
      background:#fff;
    }}
    .allocation-list {{
      display:grid;
      gap:12px;
    }}
    .allocation-copy {{
      display:flex;
      align-items:baseline;
      justify-content:space-between;
      gap:14px;
      margin-bottom:6px;
    }}
    .allocation-copy strong {{
      min-width:0;
      font-size:12px;
      line-height:1.2;
    }}
    .allocation-copy span {{
      flex:0 0 auto;
      color:var(--muted);
      font-size:11px;
      white-space:nowrap;
    }}
    .allocation-track {{
      overflow:hidden;
      height:7px;
      border-radius:999px;
      background:#f1f1f1;
    }}
    .allocation-track span {{
      display:block;
      min-width:2px;
      max-width:100%;
      height:100%;
      border-radius:inherit;
      background:linear-gradient(90deg,var(--orange),#ffb17f);
    }}
    .holdings-panel {{
      min-width:0;
      padding:17px 18px;
      border:1px solid var(--gray-light);
      border-radius:16px;
      background:#fff;
    }}
    .holdings-grid {{
      display:grid;
      gap:0;
    }}
    .holding-header,.holding-row {{
      display:grid;
      grid-template-columns:minmax(0,1fr) 82px 120px;
      gap:12px;
      align-items:center;
    }}
    .holding-header {{
      padding:9px 10px;
      border-radius:9px;
      background:#faf7f5;
      color:var(--muted);
      font-size:10px;
      font-weight:600;
      text-transform:uppercase;
      letter-spacing:.035em;
    }}
    .holding-header span:nth-child(n+2) {{ text-align:right; }}
    .holding-row {{
      min-width:0;
      padding:11px 10px;
      border-top:1px solid #ececec;
      font-size:12px;
    }}
    .holding-asset {{ min-width:0; }}
    .holding-asset strong {{
      display:block;
      overflow:hidden;
      font-size:12px;
      line-height:1.25;
      text-overflow:ellipsis;
      white-space:nowrap;
    }}
    .holding-asset span {{
      display:block;
      overflow:hidden;
      margin-top:3px;
      color:var(--muted);
      font-size:10px;
      line-height:1.2;
      text-overflow:ellipsis;
      white-space:nowrap;
    }}
    .holding-weight,.holding-value {{
      text-align:right;
      white-space:nowrap;
    }}
    .holding-value {{ font-weight:600; }}
    .empty-overview {{
      margin:0;
      color:var(--muted);
      font-size:12px;
    }}
    .table-shell {{
      overflow:visible;
      margin:0 2px;
      border:1px solid var(--gray-light);
      border-radius:16px;
      background:#fff;
    }}
    table {{
      width:100%;
      border-collapse:collapse;
      table-layout:fixed;
      background:#fff;
    }}
    th,td {{
      padding:13px 12px;
      border-right:1px solid #d8d8d8;
      border-bottom:1px solid #d8d8d8;
      vertical-align:middle;
    }}
    tr:last-child th,tr:last-child td {{ border-bottom:none; }}
    th:last-child,td:last-child {{ border-right:none; }}
    .blank {{ width:42%; background:#fff; }}
    .head {{
      padding-top:11px;
      padding-bottom:11px;
      background:var(--orange);
      color:#fff;
      text-align:center;
      font-size:13px;
      line-height:1.12;
      font-weight:700;
    }}
    .stub {{
      width:42%;
      padding-left:16px;
      padding-right:16px;
      font-size:12px;
      line-height:1.2;
      font-weight:700;
      text-align:left;
    }}
    .metric-label {{
      position:relative;
      display:inline-flex;
      align-items:center;
      gap:6px;
      min-width:0;
    }}
    .metric-help {{
      position:relative;
      display:inline-grid;
      place-items:center;
      width:16px;
      height:16px;
      flex:0 0 16px;
      padding:0;
      border:1px solid #c9c9c9;
      border-radius:50%;
      background:#fff;
      color:#777;
      font:700 10px/1 Arial,sans-serif;
      cursor:help;
    }}
    .metric-help::after {{
      position:absolute;
      z-index:8;
      left:-8px;
      bottom:calc(100% + 8px);
      width:min(240px,70vw);
      padding:8px 9px;
      border-radius:8px;
      background:#282828;
      color:#fff;
      content:attr(data-tooltip);
      font:500 11px/1.35 "Montserrat","Avenir Next",Avenir,Arial,sans-serif;
      text-align:left;
      white-space:normal;
      opacity:0;
      pointer-events:none;
      transform:translateY(3px);
      transition:opacity .12s ease,transform .12s ease;
    }}
    .metric-help:hover::after,
    .metric-help:focus-visible::after {{
      opacity:1;
      transform:translateY(0);
    }}
    .metric-value {{
      font-size:16px;
      line-height:1.08;
      font-weight:500;
      text-align:center;
      white-space:nowrap;
    }}
    .metric-value.money {{ font-size:15px; }}
    .mobile-metrics {{ display:none; }}
    .mobile-period {{
      overflow:hidden;
      border:1px solid var(--gray-light);
      border-radius:14px;
      background:#fff;
    }}
    .mobile-period + .mobile-period {{ margin-top:12px; }}
    .mobile-period-title {{
      margin:0;
      padding:11px 13px;
      background:var(--orange);
      color:#fff;
      font-size:13px;
      line-height:1.15;
      font-weight:700;
    }}
    .mobile-period-body {{ padding:2px 12px; }}
    .mobile-metric-row {{
      border-bottom:1px solid #e4e4e4;
    }}
    .mobile-metric-row:last-child {{ border-bottom:none; }}
    .mobile-metric-summary {{
      display:grid;
      grid-template-columns:minmax(0,1fr) auto;
      align-items:center;
      gap:14px;
      padding:11px 1px;
      list-style:none;
      cursor:pointer;
    }}
    .mobile-metric-summary::-webkit-details-marker {{ display:none; }}
    .mobile-metric-label {{
      display:inline-flex;
      align-items:center;
      gap:7px;
      min-width:0;
      font-size:12px;
      line-height:1.2;
      font-weight:600;
    }}
    .mobile-metric-info {{
      display:inline-grid;
      place-items:center;
      width:18px;
      height:18px;
      flex:0 0 18px;
      border:1px solid #c9c9c9;
      border-radius:50%;
      background:#fff;
      color:#777;
      font:700 10px/1 Arial,sans-serif;
    }}
    .mobile-metric-summary strong {{
      flex:0 0 auto;
      font-size:14px;
      line-height:1;
      font-weight:600;
      white-space:nowrap;
    }}
    .mobile-metric-row[open] .mobile-metric-info {{
      border-color:#f0a474;
      background:#fff6f0;
      color:#a8531f;
    }}
    .mobile-metric-explanation {{
      margin:-1px 1px 10px;
      padding:9px 10px;
      border:1px solid #eadfd8;
      border-radius:10px;
      background:#faf7f5;
      color:#5e5149;
      font-size:11px;
      line-height:1.4;
      overflow-wrap:anywhere;
    }}
    @media (min-width:601px) {{
      #chart-mode-return:checked ~ .chart-shell
        .performance-chart-desktop.chart-mode-return,
      #chart-mode-market:checked ~ .chart-shell
        .performance-chart-desktop.chart-mode-market {{
        display:block;
      }}
    }}
    @media (max-width:900px) {{
      .dashboard {{ width:min(calc(100vw - 28px),760px); }}
      .sheet {{ padding-left:20px; padding-right:20px; }}
    }}
    @media (max-width:600px) {{
      .dashboard {{
        width:100%;
        margin:0 auto;
        padding:10px 10px max(20px,env(safe-area-inset-bottom));
      }}
      .sheet {{
        padding:16px 14px 20px;
        border-radius:20px;
        box-shadow:0 8px 28px rgba(59,37,22,.07);
      }}
      .hero {{
        align-items:center;
        gap:12px;
        margin-bottom:15px;
        padding:3px 2px;
      }}
      .hero img {{ width:54px; height:54px; }}
      .hero-title {{ font-size:20px; line-height:1.04; }}
      .hero-subtitle {{ margin-top:6px; font-size:11px; line-height:1.25; }}
      .portfolio-summary {{
        align-items:flex-start;
        margin:0 0 17px;
        padding:15px 15px;
        flex-direction:column;
        gap:9px;
        border-radius:15px;
      }}
      .summary-label {{ margin-bottom:6px; font-size:11.5px; }}
      .summary-value {{ font-size:30px; }}
      .summary-context {{ text-align:left; font-size:11px; line-height:1.4; }}
      .card {{ margin-bottom:15px; }}
      .chart-head {{ padding:0 0 12px; }}
      .chart-heading-row {{ display:block; }}
      .chart-title {{ font-size:17px; line-height:1.2; }}
      .chart-subtitle {{
        max-width:none;
        margin-top:6px;
        font-size:11.5px;
        line-height:1.42;
      }}
      .chart-card .chart-current {{ display:none !important; }}
      .chart-mode-switch {{
        display:flex;
        width:100%;
        margin-top:12px;
        padding:3px;
        border-radius:11px;
      }}
      .chart-mode-button {{
        flex:1 1 50%;
        min-width:0;
        min-height:38px;
        padding:8px 5px;
        font-size:11px;
        border-radius:8px;
      }}
      .chart-mobile-summary {{
        min-height:44px;
        margin-top:11px;
        padding:10px 12px;
        border-radius:12px;
      }}
      #chart-mode-return:checked ~ .chart-head .chart-mobile-summary-return,
      #chart-mode-market:checked ~ .chart-head .chart-mobile-summary-market {{
        display:flex;
      }}
      .chart-mobile-summary strong {{ font-size:15px; }}
      #chart-mode-return:checked ~ .chart-shell
        .performance-chart-mobile.chart-mode-return,
      #chart-mode-market:checked ~ .chart-shell
        .performance-chart-mobile.chart-mode-market {{
        display:block;
      }}
      .performance-chart-mobile {{
        touch-action:none;
        overscroll-behavior:contain;
      }}
      .no-js .chart-no-js-note {{
        display:block;
        margin:8px 0 0;
        color:var(--muted);
        font-size:10.5px;
        line-height:1.35;
      }}
      .no-js .performance-chart-mobile .chart-fallback-layer {{
        display:inline;
      }}
      .no-js .chart-fallback-item:focus-within
        .chart-fallback-tooltip {{
        visibility:visible;
        opacity:1;
      }}
      .performance-chart-mobile .axis-text {{ font-size:9px; }}
      .performance-chart-mobile .quarter-text {{
        font-size:9.5px;
        letter-spacing:.01em;
      }}
      .chart-shell {{
        min-height:0;
        border-radius:13px;
      }}
      .section-title {{ margin-bottom:14px; font-size:16px; }}
      .overview-panel,.holdings-panel {{
        padding:15px 14px;
        border-radius:15px;
      }}
      .allocation-list {{ gap:14px; }}
      .allocation-copy {{
        align-items:flex-start;
        gap:8px;
        margin-bottom:7px;
      }}
      .allocation-copy strong {{ font-size:11.5px; }}
      .allocation-copy span {{ font-size:10.5px; line-height:1.25; }}
      .allocation-track {{ height:6px; }}
      .holding-header {{ display:none; }}
      .holding-row {{
        grid-template-columns:minmax(0,1fr) auto;
        gap:9px 14px;
        padding:13px 0;
      }}
      .holding-asset {{ grid-column:1 / -1; }}
      .holding-asset strong {{
        overflow:visible;
        font-size:12.5px;
        line-height:1.3;
        text-overflow:clip;
        white-space:normal;
      }}
      .holding-asset span {{ margin-top:4px; font-size:10.5px; }}
      .holding-weight,.holding-value {{
        display:flex;
        align-items:baseline;
        justify-content:space-between;
        gap:12px;
        text-align:left;
      }}
      .holding-weight::before,.holding-value::before {{
        color:var(--muted);
        content:attr(data-label);
        font-size:10px;
        font-weight:400;
      }}
      .holding-value {{ text-align:right; }}
      .table-shell {{ display:none; }}
      .mobile-metrics {{ display:block; }}
      .mobile-period {{
        border-radius:15px;
        box-shadow:0 3px 12px rgba(59,37,22,.035);
      }}
      .mobile-period + .mobile-period {{ margin-top:14px; }}
      .mobile-period-title {{
        padding:12px 14px;
        font-size:13.5px;
      }}
      .mobile-period-body {{ padding:2px 14px; }}
      .mobile-metric-summary {{
        gap:12px;
        padding:12px 1px;
      }}
      .mobile-metric-label {{
        font-size:12px;
        line-height:1.32;
      }}
      .mobile-metric-summary strong {{ font-size:14.5px; }}
      .mobile-metric-info {{
        width:22px;
        height:22px;
        flex-basis:22px;
        font-size:11px;
      }}
      .mobile-metric-explanation {{
        margin:-2px 1px 11px;
        padding:9px 10px;
      }}
    }}
    @media (max-width:360px) {{
      .dashboard {{ padding-left:7px; padding-right:7px; }}
      .sheet {{ padding-left:11px; padding-right:11px; }}
      .hero {{ gap:9px; }}
      .hero img {{ width:48px; height:48px; }}
      .hero-title {{ font-size:18px; }}
      .summary-value {{ font-size:27px; }}
      .chart-title {{ font-size:16px; }}
      .chart-mode-button {{ font-size:10.5px; }}
      .mobile-metric-summary {{ gap:8px; }}
      .mobile-metric-label {{ font-size:11.5px; }}
      .mobile-metric-summary strong {{ font-size:13px; }}
    }}
    @media print {{
      body {{ background:#fff; }}
      .dashboard {{ width:100%; padding:0; }}
      .sheet {{ border:none; box-shadow:none; }}
    }}
  </style>
</head>
<body data-renderer-version="{RENDERER_VERSION}">
  <main class="dashboard">
    <article class="sheet">
      <header class="hero">
        <img src="{escape(logo, quote=True)}" alt="Portfolio demo"/>
        <div class="hero-copy">
          <h1 class="hero-title">Ключевые показатели<br/>портфеля</h1>
          <div class="hero-subtitle">Отчёт на {report_display_date} · {escape(currency)}</div>
        </div>
      </header>

      <section class="portfolio-summary" aria-label="Сводка портфеля">
        <div>
          <p class="summary-label">Рыночная стоимость портфеля</p>
          <div id="portfolio-market-value" class="summary-value"
               data-source-field="portfolio_market_value">{escape(market_display)}</div>
        </div>
        <div class="summary-context">
          Валюта отчёта: {escape(currency)}
        </div>
      </section>

      <section class="card chart-card" aria-label="График портфеля">
        <input class="chart-mode-input" type="radio" name="chart-mode"
               id="chart-mode-return" checked/>
        <input class="chart-mode-input" type="radio" name="chart-mode"
               id="chart-mode-market"/>
        <div class="chart-head">
          <div class="chart-heading-row">
            <div class="chart-copy chart-copy-return">
              <h2 class="chart-title">Накопленная доходность портфеля</h2>
              <p class="chart-subtitle">Доходность без влияния пополнений и выводов средств</p>
            </div>
            <div class="chart-copy chart-copy-market">
              <h2 class="chart-title">Стоимость портфеля</h2>
              <p class="chart-subtitle">Рыночная стоимость портфеля по данным отчётного ряда</p>
            </div>
            <div class="chart-current chart-current-return">
              <span>Текущая доходность</span>
              <strong
                      data-source-field="performance_series.csv:last.cumulative_ttwror">{escape(last_performance_display)}</strong>
            </div>
            <div class="chart-current chart-current-market">
              <span>Текущая стоимость</span>
              <strong
                      data-source-field="performance_series.csv:last.portfolio_market_value">{escape(last_market_display)}</strong>
            </div>
          </div>
          <div class="chart-mode-switch" role="group" aria-label="Режим графика">
            <label class="chart-mode-button"
                   for="chart-mode-return">Доходность</label>
            <label class="chart-mode-button"
                   for="chart-mode-market">Стоимость портфеля</label>
          </div>
          <div class="chart-mobile-summary chart-mobile-summary-return">
            <span>Текущая доходность</span>
            <strong
                    data-source-field="performance_series.csv:last.cumulative_ttwror">{escape(last_performance_display)}</strong>
          </div>
          <div class="chart-mobile-summary chart-mobile-summary-market">
            <span>Текущая стоимость</span>
            <strong
                    data-source-field="performance_series.csv:last.portfolio_market_value">{escape(last_market_display)}</strong>
          </div>
          <div class="chart-no-js-note">
            Коснитесь нужной точки графика, чтобы увидеть дату и значение
          </div>
        </div>
        <div class="chart-shell">
          {return_desktop_chart}
          {return_mobile_chart}
          {market_desktop_chart}
          {market_mobile_chart}
        </div>
      </section>

      <section class="card overview-panel" aria-labelledby="allocation-title">
        <h2 id="allocation-title" class="section-title">Структура портфеля</h2>
        <div class="allocation-list">
          {allocation_rows}
        </div>
      </section>

      <section class="card holdings-panel" aria-labelledby="holdings-title">
        <h2 id="holdings-title" class="section-title">Крупнейшие позиции</h2>
        <div class="holdings-grid" role="table" aria-label="Пять крупнейших позиций">
          <div class="holding-header" role="row">
            <span role="columnheader">Актив</span>
            <span role="columnheader">Доля</span>
            <span role="columnheader">Стоимость</span>
          </div>
          {holding_rows}
        </div>
      </section>

      <section class="card table-card">
        <div class="table-shell">
          <table aria-label="Показатели эффективности портфеля">
            <colgroup><col style="width:42%"/><col style="width:29%"/><col style="width:29%"/></colgroup>
            <thead>
              <tr>
                <th class="blank" aria-label="Показатель"></th>
                <th class="head" scope="col">За всё время</th>
                <th class="head" scope="col">За текущий год</th>
              </tr>
            </thead>
            <tbody>
              {table_rows}
            </tbody>
          </table>
        </div>
        <div class="mobile-metrics">{mobile_metrics}</div>
      </section>
    </article>
  </main>
  <script type="application/json" id="chart-data">{chart_data}</script>
  <script>
    (() => {{
      "use strict";

      const chartRows = Object.freeze(
        JSON.parse(document.getElementById("chart-data").textContent)
          .map((row) => Object.freeze(row))
      );
      const charts = [...document.querySelectorAll(".performance-chart")];
      const clamp = (value, minimum, maximum) =>
        Math.max(minimum, Math.min(maximum, value));

      if (location.hash.startsWith("#chart-tip-")) {{
        try {{
          history.replaceState(null, "", location.href.split("#")[0]);
        }} catch (_) {{}}
      }}

      function hideTooltip(state) {{
        state.tooltip.setAttribute("hidden", "");
      }}

      function showPoint(svg, state, point) {{
        const pointX = Number(point.dataset.pointX);
        const pointY = Number(point.dataset.pointY);
        const plotLeft = Number(svg.dataset.plotLeft);
        const plotRight = Number(svg.dataset.plotRight);
        const plotTop = Number(svg.dataset.plotTop);
        const plotBottom = Number(svg.dataset.plotBottom);
        const tooltipWidth = Number(state.tooltip.dataset.tooltipWidth);
        const tooltipHeight = Number(state.tooltip.dataset.tooltipHeight);
        const preferredX = pointX + tooltipWidth + 12 <= plotRight
          ? pointX + 12
          : pointX - tooltipWidth - 12;
        const preferredY = pointY - tooltipHeight - 12 >= plotTop
          ? pointY - tooltipHeight - 12
          : pointY + 12;
        const tooltipX = clamp(
          preferredX,
          plotLeft + 4,
          plotRight - tooltipWidth - 4
        );
        const tooltipY = clamp(
          preferredY,
          plotTop + 4,
          plotBottom - tooltipHeight - 4
        );

        state.guide.setAttribute("x1", pointX);
        state.guide.setAttribute("x2", pointX);
        state.marker.setAttribute("cx", pointX);
        state.marker.setAttribute("cy", pointY);
        state.box.setAttribute(
          "transform",
          "translate(" + tooltipX + "," + tooltipY + ")"
        );
        state.date.textContent = point.dataset.displayDate;
        state.value.textContent = point.dataset.displayValue;
        state.tooltip.removeAttribute("hidden");
      }}

      function nearestPoint(state, svgX) {{
        let nearest = state.points[0];
        let distance = Math.abs(Number(nearest.dataset.pointX) - svgX);
        for (let index = 1; index < state.points.length; index += 1) {{
          const candidate = state.points[index];
          const candidateDistance = Math.abs(
            Number(candidate.dataset.pointX) - svgX
          );
          if (candidateDistance >= distance) break;
          nearest = candidate;
          distance = candidateDistance;
        }}
        return nearest;
      }}

      function showAtClientX(svg, state, clientX) {{
        const bounds = svg.getBoundingClientRect();
        if (bounds.width <= 0) return;
        const viewBox = svg.viewBox.baseVal;
        const svgX = viewBox.x +
          ((clientX - bounds.left) / bounds.width) * viewBox.width;
        showPoint(svg, state, nearestPoint(state, svgX));
      }}

      charts.forEach((svg) => {{
        if (Number(svg.dataset.pointCount) !== chartRows.length) {{
          throw new Error("Embedded chart data and SVG point counts differ");
        }}

        const state = {{
          points: [...svg.querySelectorAll(".chart-hit-point")],
          tooltip: svg.querySelector(".chart-live-tooltip"),
          guide: svg.querySelector(".tooltip-guide"),
          marker: svg.querySelector(".tooltip-marker"),
          box: svg.querySelector(".tooltip-box"),
          date: svg.querySelector(".tooltip-date"),
          value: svg.querySelector(".tooltip-value"),
          activePointerId: null,
          touchActive: false,
          touchMoved: false
        }};
        const isMobileChart = svg.classList.contains(
          "performance-chart-mobile"
        );

        svg.chartInteractionState = state;

        svg.addEventListener("pointerdown", (event) => {{
          if (isMobileChart && event.pointerType === "touch") return;
          state.activePointerId = event.pointerId;
          try {{ svg.setPointerCapture(event.pointerId); }} catch (_) {{}}
          showAtClientX(svg, state, event.clientX);
        }});
        svg.addEventListener("pointermove", (event) => {{
          if (isMobileChart && event.pointerType === "touch") return;
          if (
            event.pointerType === "mouse" ||
            state.activePointerId === event.pointerId
          ) {{
            showAtClientX(svg, state, event.clientX);
          }}
        }});
        svg.addEventListener("pointerup", (event) => {{
          if (isMobileChart && event.pointerType === "touch") return;
          if (state.activePointerId !== event.pointerId) return;
          showAtClientX(svg, state, event.clientX);
          try {{ svg.releasePointerCapture(event.pointerId); }} catch (_) {{}}
          state.activePointerId = null;
          if (event.pointerType !== "mouse") hideTooltip(state);
        }});
        svg.addEventListener("pointercancel", () => {{
          state.activePointerId = null;
          hideTooltip(state);
        }});
        svg.addEventListener("pointerleave", (event) => {{
          if (event.pointerType === "mouse" && state.activePointerId === null) {{
            hideTooltip(state);
          }}
        }});
        svg.addEventListener("contextmenu", (event) => event.preventDefault());
        svg.addEventListener("selectstart", (event) => event.preventDefault());

        if (isMobileChart) {{
          svg.addEventListener("touchstart", (event) => {{
            if (event.touches.length !== 1) return;
            state.touchActive = true;
            state.touchMoved = false;
            showAtClientX(svg, state, event.touches[0].clientX);
          }}, {{ passive: false, capture: true }});
          svg.addEventListener("touchmove", (event) => {{
            if (!state.touchActive || event.touches.length !== 1) return;
            event.preventDefault();
            state.touchMoved = true;
            showAtClientX(svg, state, event.touches[0].clientX);
          }}, {{ passive: false, capture: true }});
          svg.addEventListener("touchend", (event) => {{
            if (!state.touchActive) return;
            if (state.touchMoved && event.cancelable) event.preventDefault();
            state.touchActive = false;
            hideTooltip(state);
            if (state.touchMoved) {{
              document.documentElement.classList.remove("no-js");
              document.documentElement.classList.add("js");
            }}
          }}, {{ passive: false, capture: true }});
          svg.addEventListener("touchcancel", () => {{
            state.touchActive = false;
            state.touchMoved = false;
            hideTooltip(state);
          }});
        }}
      }});

      document.querySelectorAll(".chart-mode-input").forEach((input) => {{
        input.addEventListener("change", () => {{
          charts.forEach((svg) => hideTooltip(svg.chartInteractionState));
        }});
      }});
    }})();
  </script>
</body>
</html>
"""


def load_detailed_report(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(read_text(path, "detailed report"))
    except json.JSONDecodeError as error:
        raise DashboardError(f"detailed report is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise DashboardError("detailed report root must be an object")
    errors = validate_detailed_report(value)
    if errors:
        raise DashboardError("detailed report schema validation failed: " + "; ".join(errors))
    return value


def _detail_decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise DashboardError(f"{label} is not a decimal: {value!r}") from error
    if not result.is_finite():
        raise DashboardError(f"{label} is not finite")
    return result


def _status_badge(status: str, reason: str | None = None) -> str:
    css = re.sub(r"[^a-z0-9]+", "-", status.casefold()).strip("-")
    title = f' title="{escape(reason, quote=True)}"' if reason else ""
    return f'<span class="status status-{css}"{title}>{escape(status.replace("_", " "))}</span>'


def _client_state_text(status: str) -> str:
    """Translate an internal calculation state into client-facing Russian."""
    normalized = status.strip().upper()
    labels = {
        "PASS": "Проверено",
        "EXACT": "Актуальная цена",
        "RECONCILED": "Сверено",
        "NOT_REQUIRED": "Пересчёт не требуется",
        "CARRIED_FORWARD": "Последняя доступная цена",
        "PARTIAL": "Доступно частично",
        "MISSING": "Нет данных для расчёта",
        "UNAVAILABLE": "Нет данных",
        "HEURISTIC_ACI": "Начисленные проценты",
        "CURRENT": "В портфеле",
        "CLOSED": "Закрытая позиция",
        "UNHELD": "Нет открытой позиции",
    }
    return labels.get(normalized, normalized.replace("_", " ").capitalize())


def _client_notice(text: str, *, tone: str = "note") -> str:
    return (
        f'<p class="client-notice client-notice-{escape(tone, quote=True)}">'
        f"{escape(text)}</p>"
    )


TRANSACTION_LABELS = {
    "BUY": "Покупка",
    "SELL": "Продажа",
    "DEPOSIT": "Пополнение",
    "REMOVAL": "Вывод средств",
    "DIVIDENDS": "Дивиденды",
    "INTEREST": "Процентный доход",
    "INTEREST_CHARGE": "Процентное списание",
    "TRANSFER": "Перевод",
    "TRANSFER_IN": "Входящий перевод",
    "TRANSFER_OUT": "Исходящий перевод",
}


def _transaction_label(raw_type: str, fallback: str) -> str:
    return TRANSACTION_LABELS.get(raw_type, fallback.replace("Other /", "Другое:"))


def _ru_count(value: int, one: str, few: str, many: str) -> str:
    remainder_100 = value % 100
    remainder_10 = value % 10
    if 11 <= remainder_100 <= 14:
        word = many
    elif remainder_10 == 1:
        word = one
    elif 2 <= remainder_10 <= 4:
        word = few
    else:
        word = many
    return f"{value} {word}"


def _money_text(value: Any, currency: str) -> str:
    return format_money(_detail_decimal(value, "detailed monetary value"), currency)


def _render_taxonomy_node(node: Mapping[str, Any]) -> str:
    has_allocation = any(
        _detail_decimal(value, "taxonomy allocation") != 0
        for value in node["allocation_by_currency"].values()
    )
    children = "".join(
        markup
        for child in node["children"]
        if (markup := _render_taxonomy_node(child))
    )
    if not has_allocation and not children:
        return ""
    allocation = " · ".join(
        f"{escape(code)} {escape(_money_text(value, code))}"
        for code, value in sorted(node["allocation_by_currency"].items())
        if _detail_decimal(value, "taxonomy allocation") != 0
    )
    if children:
        return (
            f'<details class="taxonomy-node" data-node-id="{escape(node["node_id"], quote=True)}">'
            "<summary>"
            f'<span>{escape(node["name"])}</span><small>{escape(allocation)}</small>'
            f'</summary><div class="taxonomy-children">{children}</div></details>'
        )
    return (
        f'<div class="taxonomy-node taxonomy-leaf" data-node-id="{escape(node["node_id"], quote=True)}">'
        f'<span>{escape(node["name"])}</span><small>{escape(allocation)}</small></div>'
    )


CLIENT_WORKSPACE_TABS = (
    ("overview", "Обзор"),
    ("performance", "Доходность"),
    ("portfolio", "Портфель"),
    ("allocation", "Структура"),
    ("activity", "Операции"),
)


def _client_decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise DashboardError(f"client view has invalid {label}") from error
    if not result.is_finite():
        raise DashboardError(f"client view has non-finite {label}")
    return result


def _client_money(value: Any, currency: str) -> str:
    """Client money uses non-breaking grouping so values never split across lines."""
    return format_money(_client_decimal(value, "money"), currency).replace(" ", "\u00a0")


def _client_signed_money(value: Any, currency: str) -> str:
    amount = _client_decimal(value, "signed money")
    formatted = _client_money(amount, currency)
    return f"+{formatted}" if amount > 0 else formatted


def _client_result_tone(value: Any) -> str:
    amount = _client_decimal(value, "result tone")
    if amount > 0:
        return "is-positive"
    if amount < 0:
        return "is-negative"
    return "is-neutral"


def _client_compact_percent(value: Any) -> str:
    percent = (_client_decimal(value, "compact percent") * Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    text = format(percent, "f").rstrip("0").rstrip(".")
    return f"{text.replace('.', ',')}%"


def _client_series(
    mode: Mapping[str, Any], *, period: str = "all_time"
) -> list[SeriesPoint]:
    result = []
    rows = (
        mode["performance"]["series"]
        if period == "all_time"
        else mode["performance"]["period_series"][period]
    )
    for row in rows:
        date_text = str(row["date"])
        result.append(
            SeriesPoint(
                iso_date(date_text, "client performance date"),
                date_text,
                _client_decimal(row["cumulative_return"], "client cumulative return"),
                _client_decimal(row["market_value"], "client market value"),
            )
        )
    if not result:
        raise DashboardError("client performance series is empty")
    return result


def _client_fx_note(mode: Mapping[str, Any]) -> str:
    fx = mode["fx"]
    if not fx["conversion_required"]:
        return ""
    rows = fx.get("period_context", {}).get("current_year", [])
    if not rows:
        raise DashboardError("converted client mode lacks YTD FX context")
    parts = []
    for row in rows:
        start_rate = _client_decimal(row["start_rate"], "FX start rate").quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        end_rate = _client_decimal(row["end_rate"], "FX end rate").quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        movement = format_percent(_client_decimal(row["movement"], "FX movement"))
        parts.append(
            f'{row["source_currency"]}→{row["target_currency"]}: '
            f'{str(start_rate).replace(".", ",")} на '
            f'{display_date(str(row["start_observation_date"]))} → '
            f'{str(end_rate).replace(".", ",")} на '
            f'{display_date(str(row["end_observation_date"]))}; '
            f'изменение {movement}'
        )
    return (
        "Курс ЕЦБ с начала года · "
        + " · ".join(parts)
        + ". Справочно: движение курса не является разложением доходности."
    )


def _client_allocation_rows(
    mode: Mapping[str, Any],
    *,
    rows: list[Mapping[str, Any]] | None = None,
    limit: int | None = None,
) -> str:
    currency = str(mode["currency"])
    rows = list(rows if rows is not None else mode["allocation"])
    if limit is not None:
        rows = rows[:limit]
    markup = []
    for row in rows:
        name = str(row["name"])
        share = _client_decimal(row["share"], "allocation share")
        if name.casefold() in {
            "ликвидность",
            "cash",
            "liquidity",
            "денежные средства",
        }:
            name = "Остаток на счетах" if share < 0 else "Свободные средства"
        row_class = "allocation-row is-negative" if share < 0 else "allocation-row"
        bar_share = min(abs(share), Decimal(1))
        markup.append(
            f'<div class="{row_class}">'
            '<div class="allocation-copy">'
            f'<strong>{escape(name)}</strong>'
            f'<span>{escape(format_percent(share))} · {escape(_client_money(row["value"], currency))}</span>'
            '</div><div class="allocation-track" aria-hidden="true">'
            f'<span style="width:{css_percent(bar_share)}%"></span></div></div>'
        )
    return "".join(markup) or '<p class="empty-state">Нет данных о структуре</p>'


def _client_holdings(mode: Mapping[str, Any], *, limit: int | None = None) -> str:
    currency = str(mode["currency"])
    rows = list(mode["holdings"])
    if limit is not None:
        rows = rows[:limit]
    return "".join(
        '<div class="holding-row" role="row">'
        '<div class="holding-name" role="cell">'
        f'<strong>{escape(str(row["instrument"]))}</strong>'
        f'<span>{escape(str(row["classification"]))} · {escape(str(row["instrument_currency"]))}</span></div>'
        f'<div role="cell" data-label="Доля">{escape(format_percent(_client_decimal(row["share"], "holding share")))}</div>'
        f'<div role="cell" data-label="Стоимость"><strong>{escape(_client_money(row["value"], currency))}</strong></div>'
        '</div>'
        for row in rows
    ) or '<p class="empty-state">Нет открытых позиций</p>'


def _client_exposure_rows(mode: Mapping[str, Any]) -> str:
    rows = [
        row
        for row in mode["currency_exposure"]
        if _client_decimal(row["share"], "currency exposure share") != 0
    ]
    if not rows:
        return '<p class="empty-state compact-empty">Нет данных о распределении по валютам</p>'

    def copy(row: Mapping[str, Any]) -> str:
        share = _client_decimal(row["share"], "currency exposure share")
        return (
            f'<strong>{escape(str(row["currency"]))}</strong>'
            '<span aria-hidden="true">—</span>'
            f'<b>{escape(_client_compact_percent(share))}</b>'
            + (
                '<small class="currency-exposure-status">отрицательная позиция</small>'
                if share < 0
                else ""
            )
        )

    if len(rows) == 1:
        return (
            '<div class="currency-exposure-single" data-currency-exposure-row>'
            f'{copy(rows[0])}</div>'
        )
    markup = []
    for row in rows:
        share = _client_decimal(row["share"], "currency exposure share")
        row_class = (
            "currency-exposure-row is-negative"
            if share < 0
            else "currency-exposure-row"
        )
        bar_share = min(abs(share), Decimal(1))
        markup.append(
            f'<div class="{row_class}" data-currency-exposure-row>'
            f'<div class="currency-exposure-copy">{copy(row)}</div>'
            '<div class="allocation-track" aria-hidden="true">'
            f'<span style="width:{css_percent(bar_share)}%"></span></div></div>'
        )
    return "".join(markup)


def _client_geography_view(
    rows: list[Mapping[str, Any]], currency: str
) -> str:
    marker_specs = {
        "ca": (137, 88, "CA", -25, -20),
        "us": (151, 128, "US", 30, 20),
        "br": (232, 236, "BR", 0, 0),
        "dk": (352, 91, "DK", 35, -15),
        "gb": (329, 103, "GB", -30, 15),
    }
    countries = [
        country
        for row in rows
        for country in row.get("countries", [])
        if _client_decimal(country["share"], "country share") != 0
    ]
    global_row = next(
        (
            row
            for row in rows
            if str(row.get("region_key")) == "global"
            and _client_decimal(row["share"], "geography share") != 0
        ),
        None,
    )
    regional_rows = [
        row
        for row in rows
        if str(row.get("region_key")) not in {"global", "other"}
        and row.get("instruments")
        and not row.get("countries")
        and _client_decimal(row["share"], "geography share") != 0
    ]
    if not countries and global_row is None and not regional_rows:
        return '<p class="empty-state">Нет подтверждённых данных о географии</p>'

    def detail_panel(
        item: Mapping[str, Any], target_key: str, *, eyebrow: str
    ) -> str:
        instruments = "".join(
            '<li><span><strong>'
            f'{escape(str(instrument["name"]))}</strong><small>Исходная валюта · '
            f'{escape(str(instrument["instrument_currency"]))}</small></span>'
            f'<b>{escape(_client_money(instrument["value"], currency))}</b></li>'
            for instrument in item.get("instruments", [])
        )
        return (
            '<section class="geography-detail" hidden '
            f'data-geo-detail="{escape(target_key, quote=True)}">'
            f'<p>{escape(eyebrow)}</p><h4>{escape(str(item["name"]))}</h4>'
            '<div class="geography-detail-total">'
            f'<strong>{escape(_client_compact_percent(item["share"]))}</strong>'
            f'<span>{escape(_client_money(item["value"], currency))}</span></div>'
            '<small>Доля от инвестированной части · сумма в валюте отчёта</small>'
            f'<ul>{instruments}</ul></section>'
        )

    def no_js_detail(item: Mapping[str, Any], *, eyebrow: str) -> str:
        instruments = "".join(
            '<li><span><strong>'
            f'{escape(str(instrument["name"]))}</strong><small>Исходная валюта · '
            f'{escape(str(instrument["instrument_currency"]))}</small></span>'
            f'<b>{escape(_client_money(instrument["value"], currency))}</b></li>'
            for instrument in item.get("instruments", [])
        )
        return (
            '<details class="geography-no-js-detail">'
            '<summary><span><small>'
            f'{escape(eyebrow)}</small><strong>{escape(str(item["name"]))}</strong></span>'
            f'<b>{escape(_client_compact_percent(item["share"]))}</b></summary>'
            '<div class="geography-no-js-body"><div class="geography-detail-total">'
            f'<strong>{escape(_client_compact_percent(item["share"]))}</strong>'
            f'<span>{escape(_client_money(item["value"], currency))}</span></div>'
            '<small>Доля от инвестированной части · сумма в валюте отчёта</small>'
            f'<ul>{instruments}</ul></div></details>'
        )

    markers = []
    details = []
    unmapped_countries = []
    for country in countries:
        key = str(country.get("country_key", ""))
        details.append(detail_panel(country, key, eyebrow="Страна"))
        spec = marker_specs.get(key)
        if spec is None:
            unmapped_countries.append(country)
            continue
        x, y, short_label, hit_x, hit_y = spec
        instrument_names = ", ".join(
            str(item["name"]) for item in country.get("instruments", [])
        )
        accessible = (
            f'{country["name"]}: {_client_compact_percent(country["share"])}, '
            f'{_client_money(country["value"], currency)}. {instrument_names}'
        )
        markers.append(
            '<g class="country-marker" role="button" tabindex="0" '
            f'data-geo-target="{escape(key, quote=True)}" aria-expanded="false" '
            f'aria-label="{escape(accessible, quote=True)}" transform="translate({x} {y})">'
            f'<circle class="country-marker-hit" cx="{hit_x}" cy="{hit_y}" r="52"/>'
            '<circle class="country-marker-halo" r="16"/>'
            '<circle class="country-marker-dot" r="9"/>'
            f'<text class="country-marker-label" y="-22">{escape(short_label)}</text></g>'
        )
    if global_row is not None:
        details.append(detail_panel(global_row, "global", eyebrow="Глобальная экспозиция"))
    for row in regional_rows:
        details.append(
            detail_panel(
                row,
                f'region-{row["region_key"]}',
                eyebrow="Региональная экспозиция",
            )
        )

    no_js_details = [no_js_detail(country, eyebrow="Страна") for country in countries]
    if global_row is not None:
        no_js_details.append(no_js_detail(global_row, eyebrow="Глобальная экспозиция"))
    no_js_details.extend(
        no_js_detail(row, eyebrow="Региональная экспозиция")
        for row in regional_rows
    )

    legend = "".join(
        '<div class="geography-legend-row" role="listitem" '
        f'data-region-key="{escape(str(row.get("region_key", "other")), quote=True)}">'
        '<span class="geography-swatch" aria-hidden="true"></span><strong>'
        f'{escape(str(row["name"]))}</strong><span>'
        f'{escape(_client_compact_percent(row["share"]))}</span></div>'
        for row in rows
        if _client_decimal(row["share"], "geography share") != 0
    )
    def supplemental_button(
        item: Mapping[str, Any], target_key: str, subtitle: str
    ) -> str:
        return (
            '<button class="global-instruments-card" type="button" '
            f'data-geo-target="{escape(target_key, quote=True)}" aria-expanded="false">'
            '<span class="global-icon" aria-hidden="true">◎</span><span><strong>'
            f'{escape(str(item["name"]))}</strong><small>{escape(subtitle)}</small></span>'
            f'<b>{escape(_client_compact_percent(item["share"]))}</b></button>'
        )
    supplemental_buttons = "".join(
        supplemental_button(country, str(country["country_key"]), "Страна без отдельного маркера")
        for country in unmapped_countries
    )
    supplemental_buttons += "".join(
        supplemental_button(
            row,
            f'region-{row["region_key"]}',
            "Регион без подтверждённой страны",
        )
        for row in regional_rows
    )
    if global_row is not None:
        supplemental_buttons += supplemental_button(
            global_row,
            "global",
            "Вне привязки к одной стране",
        )
    return (
        '<div class="geography-layout" data-geography-map>'
        '<figure class="world-map-card">'
        '<svg class="world-map" viewBox="0 0 680 340" role="img" '
        'aria-label="Карта стран, подтверждённых классификацией инструментов">'
        '<defs><linearGradient id="map-ocean-fill" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#fffaf7"/><stop offset="1" stop-color="#fff"/>'
        '</linearGradient></defs>'
        '<rect class="map-ocean" x="4" y="4" width="672" height="332" rx="24"/>'
        '<g class="map-graticule" aria-hidden="true">'
        '<path d="M30 112H650M30 228H650M175 28V312M340 28V312M505 28V312"/>'
        '</g><g class="map-land" aria-hidden="true">'
        '<path d="M54 70 81 46 113 35 157 39 184 51 208 73 205 91 187 98 174 116 156 126 139 121 128 132 109 122 99 105 77 99 63 87Z"/>'
        '<path d="M166 37 184 23 205 25 213 39 196 51 176 50Z"/>'
        '<path d="M133 127 154 127 170 142 163 153 178 165 170 176 153 162 146 145Z"/>'
        '<path d="M177 171 205 177 233 201 246 230 238 258 221 293 205 305 199 274 184 250 178 220 164 194Z"/>'
        '<path d="M314 81 330 70 348 74 358 84 350 96 333 98 320 92Z"/>'
        '<path d="M315 105 340 101 368 114 382 144 375 178 359 214 340 245 324 231 319 201 306 173 300 139Z"/>'
        '<path d="M358 78 389 57 431 43 483 46 535 61 587 83 614 106 600 126 572 128 552 143 522 138 500 151 468 142 447 124 416 122 391 107 370 103Z"/>'
        '<path d="M468 153 489 158 501 181 491 204 478 190 475 169Z"/>'
        '<path d="M520 225 550 212 585 219 606 239 589 261 554 268 524 253 507 239Z"/>'
        '<path d="M615 275 628 269 638 276 629 286Z"/>'
        '</g><g class="country-marker-layer">'
        + "".join(markers)
        + '</g></svg>'
        '<figcaption>Наведите, нажмите или перейдите клавишей Tab на маркер страны.</figcaption>'
        '</figure><aside class="geography-side">'
        '<div class="geography-detail-shell" aria-live="polite">'
        '<div class="geography-detail-empty" data-geo-empty><span aria-hidden="true">⌖</span>'
        '<strong>Выберите страну</strong><small>Покажем инструменты, долю и сумму</small></div>'
        + "".join(details)
        + '</div>'
        + supplemental_buttons
        + f'<div class="geography-legend" role="list" aria-label="Доли по регионам">{legend}</div>'
        + '</aside></div><div class="geography-no-js-list">'
        + "".join(no_js_details)
        + '</div>'
    )


def _client_activity(mode: Mapping[str, Any]) -> str:
    currency = str(mode["currency"])
    activity_rows = list(mode["activity"]["rows"])
    rows = "".join(
        '<tr class="activity-row" '
        f'data-action="{escape(str(row["action"]).casefold(), quote=True)}" '
        f'data-search="{escape((str(row["action"]) + " " + str(row["instrument"])).casefold(), quote=True)}">'
        f'<td>{escape(display_date(str(row["date"])))}</td>'
        f'<td>{escape(str(row["action"]))}</td>'
        f'<td>{escape(str(row["instrument"])) or "—"}</td>'
        f'<td class="money-cell">{escape(_client_money(row["amount"], currency))}</td>'
        '</tr>'
        for row in activity_rows
    )
    action_names = sorted({str(row["action"]) for row in activity_rows})
    options = "".join(
        f'<option value="{escape(name.casefold(), quote=True)}">{escape(name)}</option>'
        for name in action_names
    )

    no_js_groups = "".join(
        '<details class="activity-no-js-group"><summary><span>'
        f'{escape(name)}</span><b>{sum(1 for row in activity_rows if str(row["action"]) == name)}</b></summary>'
        '<div class="activity-no-js-rows">'
        + "".join(
            '<div class="activity-no-js-row"><time>'
            f'{escape(display_date(str(row["date"])))}</time><span>'
            f'{escape(str(row["instrument"])) or "—"}</span><strong>'
            f'{escape(_client_money(row["amount"], currency))}</strong></div>'
            for row in activity_rows
            if str(row["action"]) == name
        )
        + '</div></details>'
        for name in action_names
    )

    def totals(items: list[Mapping[str, Any]], empty: str) -> str:
        if not items:
            return f'<p class="empty-state compact-empty">{escape(empty)}</p>'
        return "".join(
            '<div class="total-row">'
            f'<span>{escape(str(item["category"]))}</span>'
            f'<strong>{escape(_client_money(item["amount"], currency))}</strong></div>'
            for item in items
            if _client_decimal(item["amount"], "activity total") != 0
        ) or f'<p class="empty-state compact-empty">{escape(empty)}</p>'

    return (
        '<div class="activity-totals">'
        '<article><h3>Доходы</h3>'
        + totals(mode["activity"]["income"], "Доходов за период не было")
        + '</article><article><h3>Комиссии и налоги</h3>'
        + totals(mode["activity"]["fees_taxes"], "Списаний за период не было")
        + '</article></div>'
        + '<div class="activity-js-content">'
        '<div class="activity-filters" role="search" aria-label="Фильтры операций">'
        '<label>Поиск<input type="search" data-activity-filter="search" placeholder="Операция или инструмент"/></label>'
        '<label>Тип<select data-activity-filter="action"><option value="">Все операции</option>'
        + options
        + '</select></label><span class="result-count" data-activity-count></span></div>'
        '<div class="table-scroll activity-table-shell"><table class="activity-table"><thead><tr>'
        '<th>Дата</th><th>Операция</th><th>Инструмент</th><th>Сумма</th>'
        f'</tr></thead><tbody>{rows}</tbody></table></div>'
        '<p class="activity-empty" data-activity-empty hidden>Нет операций по выбранным условиям</p></div>'
        f'<div class="activity-no-js-list">{no_js_groups}</div>'
    )


def _client_mode_panel(
    mode: Mapping[str, Any],
    panel: str,
    *,
    return_domains: Mapping[tuple[str, bool], tuple[float, float, float]] | None = None,
    scope_key: str = "full",
) -> str:
    currency = str(mode["currency"])
    scope_prefix = f"{scope_key}-" if scope_key else ""
    summary = mode["summary"]
    report_date = display_date(str(mode["report_date"]))
    fx_note = escape(_client_fx_note(mode))
    if panel == "overview":
        overview_fx = (
            f'<p class="fx-note" data-fx-note>{fx_note}</p>'
            if mode["fx"]["conversion_required"]
            else ""
        )
        return (
            '<div class="panel-head overview-panel-head" data-overview-block="report-context"><div>'
            '<h2>Обзор</h2></div>'
            f'<span class="as-of">На {escape(report_date)} · {escape(currency)}</span></div>'
            '<section class="overview-hero" aria-label="Стоимость портфеля" data-overview-block="total-value">'
            '<span>Стоимость портфеля</span>'
            f'<strong data-visible-total>{escape(_client_money(summary["total_value"], currency))}</strong>'
            f'<small>Валюта отчёта: <b data-visible-currency>{escape(currency)}</b></small></section>'
            '<div class="metric-grid">'
            f'<article class="return-card {_client_result_tone(summary["all_time_profit"])}" data-overview-block="all-time"><span class="metric-period">За всё время</span><strong class="metric-percent">{escape(format_percent(_client_decimal(summary["all_time_return"], "all-time return")))}</strong><p class="metric-result"><span>Результат</span><strong>{escape(_client_signed_money(summary["all_time_profit"], currency))}</strong></p></article>'
            f'<article class="return-card {_client_result_tone(summary["ytd_profit"])}" data-overview-block="ytd"><span class="metric-period">С начала года</span><strong class="metric-percent">{escape(format_percent(_client_decimal(summary["ytd_return"], "YTD return")))}</strong><p class="metric-result"><span>Результат</span><strong>{escape(_client_signed_money(summary["ytd_profit"], currency))}</strong></p></article>'
            f'<article class="liquidity-card" data-overview-block="liquidity"><span>Свободные средства</span><strong>{escape(_client_money(summary["liquidity"], currency))}</strong></article>'
            '</div><div class="overview-grid">'
            f'<article class="content-card" data-overview-block="allocation"><h3>Структура портфеля</h3>{_client_allocation_rows(mode, limit=5)}<h4>Валюты портфеля <span class="currency-exposure-inline-note">· бумаги и свободные средства</span></h4><div data-overview-block="currency-exposure">{_client_exposure_rows(mode)}</div></article>'
            f'<article class="content-card" data-overview-block="largest-holdings"><h3>Крупнейшие позиции</h3><div class="holding-list compact-holdings" role="table">{_client_holdings(mode, limit=5)}</div></article>'
            '</div>'
            f'{overview_fx}'
        )
    if panel == "performance":
        control_prefix = f'dashboard-performance-{scope_prefix}{currency.lower()}'
        metric_return_id = f'{control_prefix}-metric-return'
        metric_market_id = f'{control_prefix}-metric-market'
        period_all_time_id = f'{control_prefix}-period-all-time'
        period_ytd_id = f'{control_prefix}-period-ytd'
        chart_views = []
        for period, period_label in (("all_time", "За всё время"), ("ytd", "С начала года")):
            series = downsample_points(_client_series(mode, period=period))
            for metric, metric_label_text in (("return", "Доходность"), ("market", "Стоимость портфеля")):
                final = (
                    format_percent(series[-1].cumulative_ttwror)
                    if metric == "return"
                    else _client_money(series[-1].portfolio_market_value, currency)
                )
                chart_views.append(
                    f'<article class="performance-chart-view" data-performance-view="{metric}-{period}" '
                    + ("" if metric == "return" and period == "all_time" else "hidden")
                    + '><div class="chart-view-head">'
                    f'<div><span>{escape(period_label)}</span><h3>{escape(metric_label_text)}</h3></div>'
                    f'<strong data-performance-final>{escape(final)}</strong></div>'
                    f'<div class="performance-chart-frame" role="group" tabindex="0" aria-label="Интерактивный график: {escape(metric_label_text, quote=True)}, {escape(period_label, quote=True)}">'
                    f'{render_chart(series, metric=metric, currency=currency, period=period, focusable=False, y_domain=return_domains[(period, False)] if metric == "return" and return_domains is not None else None)}'
                    f'{render_chart(series, metric=metric, currency=currency, period=period, mobile=True, focusable=False, y_domain=return_domains[(period, True)] if metric == "return" and return_domains is not None else None)}'
                    '</div></article>'
                )
        return (
            f'<input class="dashboard-state" type="radio" name="{control_prefix}-metric" id="{metric_return_id}" checked/>'
            f'<input class="dashboard-state" type="radio" name="{control_prefix}-metric" id="{metric_market_id}"/>'
            f'<input class="dashboard-state" type="radio" name="{control_prefix}-period" id="{period_all_time_id}" checked/>'
            f'<input class="dashboard-state" type="radio" name="{control_prefix}-period" id="{period_ytd_id}"/>'
            '<div class="panel-head"><div><p class="eyebrow">Динамика портфеля</p><h2>Доходность</h2></div>'
            f'<span class="as-of">{escape(currency)} · {escape(report_date)}</span></div>'
            '<div class="performance-toolbar">'
            '<div class="segmented" role="group" aria-label="Показатель графика" data-js-inner-controls>'
            '<button type="button" data-performance-metric="return" aria-pressed="true">Доходность</button>'
            '<button type="button" data-performance-metric="market" aria-pressed="false">Стоимость</button></div>'
            '<div class="segmented no-js-inner-controls" role="group" aria-label="Показатель графика">'
            f'<label for="{metric_return_id}">Доходность</label>'
            f'<label for="{metric_market_id}">Стоимость</label></div>'
            '<div class="segmented" role="group" aria-label="Период графика" data-js-inner-controls>'
            '<button type="button" data-performance-period="all_time" aria-pressed="true">Всё время</button>'
            '<button type="button" data-performance-period="ytd" aria-pressed="false">С начала года</button></div>'
            '<div class="segmented no-js-inner-controls" role="group" aria-label="Период графика">'
            f'<label for="{period_all_time_id}">Всё время</label>'
            f'<label for="{period_ytd_id}">С начала года</label></div></div>'
            '<div class="performance-workspace">'
            + "".join(chart_views)
            + '</div>'
            f'<p class="fx-note" data-fx-note>{fx_note}</p>'
        )
    if panel == "portfolio":
        position_count = _ru_count(
            len(mode["holdings"]),
            "открытая позиция",
            "открытые позиции",
            "открытых позиций",
        )
        return (
            '<div class="panel-head"><div><p class="eyebrow">Состав активов</p><h2>Портфель</h2>'
            '</div>'
            f'<span class="as-of">{escape(position_count)} · денежные счета отдельно · '
            f'оценка в {escape(currency)}</span></div>'
            '<div class="holding-list" role="table" aria-label="Позиции портфеля">'
            '<div class="holding-row holding-header" role="row"><div role="columnheader">Инструмент</div><div role="columnheader">Доля</div><div role="columnheader">Стоимость</div></div>'
            f'{_client_holdings(mode)}</div><p class="fx-note" data-fx-note>{fx_note}</p>'
        )
    if panel == "allocation":
        control_prefix = f'dashboard-allocation-{scope_prefix}{currency.lower()}'
        asset_class_id = f'{control_prefix}-asset-class'
        sector_id = f'{control_prefix}-sector'
        geography_id = f'{control_prefix}-geography'
        exposure = _client_exposure_rows(mode)
        allocation_views = []
        for key, label in (
            ("asset_class", "Классы активов"),
            ("sector", "Сектора"),
            ("geography", "География"),
        ):
            rows = mode["allocation_views"][key]
            content = (
                _client_geography_view(rows, currency)
                if key == "geography"
                else _client_allocation_rows(mode, rows=rows)
            )
            allocation_views.append(
                f'<article class="content-card allocation-view" data-allocation-view="{key}"'
                + ("" if key == "asset_class" else " hidden")
                + f'><h3>{escape(label)}</h3><div'
                + ('' if key == "geography" else ' class="allocation-row-grid"')
                + f' data-allocation-rows="{key}">{content}</div></article>'
            )
        return (
            f'<input class="dashboard-state" type="radio" name="{control_prefix}" id="{asset_class_id}" checked/>'
            f'<input class="dashboard-state" type="radio" name="{control_prefix}" id="{sector_id}"/>'
            f'<input class="dashboard-state" type="radio" name="{control_prefix}" id="{geography_id}"/>'
            '<div class="panel-head"><div><p class="eyebrow">Распределение</p><h2>Структура</h2>'
            '</div>'
            f'<span class="as-of">{escape(currency)} · {escape(report_date)}</span></div>'
            '<div class="allocation-toolbar segmented" role="group" aria-label="Вид структуры" data-js-inner-controls>'
            '<button type="button" data-allocation-key="asset_class" aria-pressed="true">Классы активов</button>'
            '<button type="button" data-allocation-key="sector" aria-pressed="false">Сектора</button>'
            '<button type="button" data-allocation-key="geography" aria-pressed="false">География</button></div>'
            '<div class="allocation-toolbar segmented no-js-inner-controls" role="group" aria-label="Вид структуры">'
            f'<label for="{asset_class_id}">Классы активов</label>'
            f'<label for="{sector_id}">Сектора</label>'
            f'<label for="{geography_id}">География</label></div>'
            '<div class="allocation-workspace">'
            + "".join(allocation_views)
            + '</div><article class="currency-exposure-strip"><div class="currency-exposure-heading"><h3>Валюты портфеля</h3><p>Бумаги и свободные средства по исходной валюте</p></div><div>'
            + exposure
            + '</div></article>'
            f'<p class="fx-note" data-fx-note>{fx_note}</p>'
        )
    if panel == "activity":
        return (
            '<div class="panel-head"><div><p class="eyebrow">История движений</p><h2>Операции</h2>'
            '<p>Покупки, продажи, пополнения, доходы и списания</p></div>'
            f'<span class="as-of">{len(mode["activity"]["rows"])} операций · {escape(currency)}</span></div>'
            f'{_client_activity(mode)}<p class="fx-note" data-fx-note>{fx_note}</p>'
        )
    raise DashboardError(f"unsupported client panel: {panel}")


def render_client_workspace(
    report: Mapping[str, Any],
    detailed: Mapping[str, Any],
    logo: str,
    report_source: Path,
    series_source: Path,
    detailed_source: Path,
    client_views: Mapping[str, Any],
) -> str:
    raw_available = client_views.get("available_currencies")
    matrix = client_views.get("group_matrix")
    legacy_modes = client_views.get("modes")
    if matrix is None and isinstance(legacy_modes, Mapping):
        matrix = {
            "default_group": "FULL_XML",
            "available_groups": [{"id": "FULL_XML", "name": "Весь портфель"}],
            "groups": {
                "FULL_XML": {
                    "id": "FULL_XML",
                    "name": "Весь портфель",
                    "modes": legacy_modes,
                }
            },
        }
    if (
        not isinstance(raw_available, list)
        or not raw_available
        or any(
            not isinstance(currency, str)
            or re.fullmatch(r"[A-Z]{3}", currency) is None
            for currency in raw_available
        )
        or len(raw_available) != len(set(raw_available))
        or not isinstance(matrix, Mapping)
    ):
        raise DashboardError("client view reporting modes are incomplete or invalid")
    available = list(raw_available)
    raw_groups = matrix.get("available_groups")
    groups = matrix.get("groups")
    default_group = matrix.get("default_group")
    if (
        not isinstance(raw_groups, list)
        or not raw_groups
        or not isinstance(groups, Mapping)
        or any(not isinstance(item, Mapping) or not isinstance(item.get("id"), str) or not isinstance(item.get("name"), str) for item in raw_groups)
        or len({str(item["id"]) for item in raw_groups}) != len(raw_groups)
        or set(groups) != {str(item["id"]) for item in raw_groups}
        or default_group not in groups
    ):
        raise DashboardError("client view group matrix is incomplete or invalid")
    group_ids = [str(item["id"]) for item in raw_groups]
    group_names = {str(item["id"]): str(item["name"]) for item in raw_groups}
    group_slugs = {group_id: f"group-{index}" for index, group_id in enumerate(group_ids)}
    for group_id in group_ids:
        group_modes = groups[group_id].get("modes") if isinstance(groups[group_id], Mapping) else None
        if (
            not isinstance(group_modes, Mapping)
            or set(group_modes) != set(available)
            or any(not isinstance(group_modes[currency], Mapping) or group_modes[currency].get("currency") != currency for currency in available)
        ):
            raise DashboardError("client view group matrix cell is unavailable")
    default_currency = client_views.get("default_currency")
    if (
        not isinstance(default_currency, str)
        or re.fullmatch(r"[A-Z]{3}", default_currency) is None
        or default_currency not in available
        or available[0] != default_currency
    ):
        raise DashboardError("client view default currency is unavailable")
    modes = groups[default_group]["modes"]
    report_date = str(modes[default_currency]["report_date"])
    return_domains_by_group = {
        group_id: {
            (period, mobile): chart_y_domain(
                [downsample_points(_client_series(groups[group_id]["modes"][currency], period=period)) for currency in available],
                metric="return",
                mobile=mobile,
            )
            for period in ("all_time", "ytd")
            for mobile in (False, True)
        }
        for group_id in group_ids
    }
    state_inputs = (
        "".join(
            '<input class="dashboard-state" type="radio" name="dashboard-group" '
            f'id="dashboard-{group_slugs[group_id]}"'
            + (" checked" if group_id == default_group else "")
            + "/>"
            for group_id in group_ids
        )
        + "".join(
            '<input class="dashboard-state" type="radio" name="dashboard-currency" '
            f'id="dashboard-currency-{currency.lower()}"'
            + (" checked" if currency == default_currency else "")
            + "/>"
            for currency in available
        )
        + "".join(
            f'<input class="dashboard-state" type="radio" name="dashboard-tab" id="dashboard-tab-{key}"'
            + (" checked" if key == "overview" else "")
            + "/>"
            for key, _ in CLIENT_WORKSPACE_TABS
        )
    )
    tabs = "".join(
        f'<label role="tab" id="tab-{key}" aria-controls="panel-{key}" '
        f'aria-selected="{"true" if key == "overview" else "false"}" tabindex="{0 if key == "overview" else -1}" '
        f'data-tab="{key}" for="dashboard-tab-{key}">{escape(label)}</label>'
        for key, label in CLIENT_WORKSPACE_TABS
    )
    initial_panels = "".join(
        f'<section role="tabpanel" id="panel-{key}" aria-labelledby="tab-{key}" '
        f'class="dashboard-panel" data-panel="{key}" tabindex="0"'
        + ("" if key == "overview" else " hidden")
        + f'>{_client_mode_panel(modes[default_currency], key, return_domains=return_domains_by_group[default_group], scope_key="")}</section>'
        for key, _ in CLIENT_WORKSPACE_TABS
    )
    fallback_mains = "".join(
        f'<main id="dashboard-content-{group_slugs[group_id]}-{currency.lower()}" '
        f'class="layout shell no-js-currency-panels" data-no-js-cell="{escape(group_id, quote=True)}:{currency}">'
        + "".join(
            f'<section class="dashboard-panel" data-fallback-panel="{key}">'
            f'{_client_mode_panel(groups[group_id]["modes"][currency], key, return_domains=return_domains_by_group[group_id], scope_key="" if group_id == default_group else group_slugs[group_id])}</section>'
            for key, _ in CLIENT_WORKSPACE_TABS
        )
        + "</main>"
        for group_id in group_ids
        for currency in available
        if group_id != default_group or currency != default_currency
    )
    templates = "".join(
        f'<template data-mode-template="{currency}" data-panel-template="{key}" data-group-template="{escape(group_id, quote=True)}">'
        f'{_client_mode_panel(groups[group_id]["modes"][currency], key, return_domains=return_domains_by_group[group_id], scope_key="" if group_id == default_group else group_slugs[group_id])}</template>'
        for group_id in group_ids
        for currency in available
        for key, _ in CLIENT_WORKSPACE_TABS
    )
    client_views_embedded = script_safe_json(client_views)
    currency_options = "".join(
        f'<option value="{currency}">{currency}</option>' for currency in available
    )
    no_js_currency_options = "".join(
        f'<label for="dashboard-currency-{currency.lower()}">{currency}</label>'
        for currency in available
    )
    group_options = "".join(
        f'<option value="{escape(group_id, quote=True)}">{escape(group_names[group_id])}</option>'
        for group_id in group_ids
    )
    no_js_group_options = "".join(
        f'<label for="dashboard-{group_slugs[group_id]}">{escape(group_names[group_id])}</label>'
        for group_id in group_ids
    )
    cell_main_id = {
        (group_id, currency): (
            "dashboard-content" if group_id == default_group and currency == default_currency
            else f"dashboard-content-{group_slugs[group_id]}-{currency.lower()}"
        )
        for group_id in group_ids for currency in available
    }
    no_js_currency_display = ",".join(
        f'.no-js #dashboard-{group_slugs[group_id]}:checked~#dashboard-currency-{currency.lower()}:checked~#{cell_main_id[(group_id, currency)]}'
        for group_id in group_ids for currency in available
    ) + "{display:block}"
    no_js_panel_display = ",".join(
        f'.no-js #dashboard-tab-{key}:checked~#{cell_main_id[(group_id, currency)]} '
        f'.dashboard-panel[data-{("panel" if group_id == default_group and currency == default_currency else "fallback-panel")}="{key}"]'
        for group_id in group_ids for currency in available
        for key, _ in CLIENT_WORKSPACE_TABS
    ) + "{display:block!important}"
    no_js_active_currency = ",".join(
        f'.no-js #dashboard-currency-{currency.lower()}:checked~.app-header '
        f'label[for="dashboard-currency-{currency.lower()}"]'
        for currency in available
    ) + "{background:var(--orange);color:#23140b}"
    no_js_active_group = ",".join(
        f'.no-js #dashboard-{group_slugs[group_id]}:checked~.app-header label[for="dashboard-{group_slugs[group_id]}"]'
        for group_id in group_ids
    ) + "{background:var(--orange);color:#23140b}"
    no_js_inner_rules: list[str] = []
    for group_id in group_ids:
      for currency in available:
        currency_slug = currency.lower()
        scope_slug = "" if group_id == default_group else f"{group_slugs[group_id]}-"
        performance_prefix = f"dashboard-performance-{scope_slug}{currency_slug}"
        metric_ids = {
            "return": f"{performance_prefix}-metric-return",
            "market": f"{performance_prefix}-metric-market",
        }
        period_ids = {
            "all_time": f"{performance_prefix}-period-all-time",
            "ytd": f"{performance_prefix}-period-ytd",
        }
        for control_id in (*metric_ids.values(), *period_ids.values()):
            no_js_inner_rules.append(
                f'.no-js #{control_id}:checked~.performance-toolbar '
                f'label[for="{control_id}"]'
                "{background:#fff;color:var(--ink);box-shadow:0 2px 8px rgba(40,30,24,.09)}"
            )
        for metric, metric_id in metric_ids.items():
            for period, period_id in period_ids.items():
                no_js_inner_rules.append(
                    f'.no-js #{metric_id}:checked~#{period_id}:checked~.performance-workspace '
                    f'[data-performance-view="{metric}-{period}"]'
                    "{display:block!important}"
                )

        allocation_prefix = f"dashboard-allocation-{scope_slug}{currency_slug}"
        allocation_ids = {
            "asset_class": f"{allocation_prefix}-asset-class",
            "sector": f"{allocation_prefix}-sector",
            "geography": f"{allocation_prefix}-geography",
        }
        for key, control_id in allocation_ids.items():
            no_js_inner_rules.append(
                f'.no-js #{control_id}:checked~.allocation-toolbar '
                f'label[for="{control_id}"]'
                "{background:#fff;color:var(--ink);box-shadow:0 2px 8px rgba(40,30,24,.09)}"
            )
            no_js_inner_rules.append(
                f'.no-js #{control_id}:checked~.allocation-workspace '
                f'[data-allocation-view="{key}"]'
                "{display:block!important}"
            )
    no_js_inner_style = "".join(no_js_inner_rules)
    embedded = script_safe_json(
        {
            "presentation_contract": "1.0",
            "schema_version": detailed["schema_version"],
            "report_date": report_date,
            "reporting_currency": default_currency,
            "source_sha256": detailed["metadata"]["source_sha256"],
            "financial_status": detailed["sections"]["currencies"]["consolidated"]["status"],
            "section_counts": {
                "holdings": len(detailed["sections"]["holdings"]["rows"]),
                "transactions": len(detailed["sections"]["transactions"]["rows"]),
                "accounts": len(detailed["sections"]["accounts"]["rows"]),
                "securities": len(detailed["sections"]["securities"]["rows"]),
                "taxonomies": len(detailed["sections"]["allocation"]["taxonomies"]),
            },
        }
    )
    style = """
    :root{--orange:#ff914c;--orange-dark:#8b330b;--orange-soft:#ffdfca;--peach:#fff7f2;--paper:#fff;--ink:#151310;--muted:#6d6761;--line:#e4e1de;--shadow:0 18px 45px rgba(55,34,20,.08)}
    *{box-sizing:border-box}html{background:#fffaf7}body{margin:0;min-width:320px;overflow-x:hidden;background:linear-gradient(145deg,#fff 0%,#fff8f3 55%,#fff 100%);color:var(--ink);font-family:"Montserrat","Avenir Next",Avenir,Arial,sans-serif;line-height:1.45;-webkit-text-size-adjust:100%}button,input,select{font:inherit}button,select,input{touch-action:manipulation}.skip-link{position:absolute;left:-9999px;top:8px}.skip-link:focus{left:8px;z-index:40;background:#fff;padding:12px;border:2px solid var(--orange-dark);border-radius:10px}.shell{width:min(1180px,calc(100% - 32px));margin:auto}
    .app-header{padding:18px 0 14px}.header-inner{display:flex;align-items:center;gap:16px}.brand-logo{display:block;width:74px;height:58px;object-fit:contain;flex:0 0 auto}.header-copy{min-width:0}.header-copy h1{margin:0;font-size:clamp(22px,3vw,32px);line-height:1.04;letter-spacing:-.035em}.header-copy p{margin:5px 0 0;color:var(--muted);font-size:13px}.scope-controls{margin-left:auto;display:flex;align-items:end;gap:10px}.scope-control{display:grid;min-width:0;gap:4px;color:var(--muted);font-size:10px;font-weight:750}.scope-control small{font-size:9px;font-weight:500}.scope-control select{min-width:110px;min-height:44px;padding:0 12px;border:1px solid var(--line);border-radius:12px;background:#fff;color:var(--ink);font-size:12px;font-weight:800}.group-control select{min-width:190px}.no-js-options{display:none;flex-wrap:wrap;gap:3px;padding:3px;border:1px solid var(--line);border-radius:12px;background:#fff}.no-js-options label{display:grid;min-height:44px;place-items:center;padding:0 10px;border-radius:9px;cursor:pointer}
    .workspace-nav{position:sticky;top:0;z-index:20;border-block:1px solid rgba(228,225,222,.95);background:rgba(255,255,255,.95);box-shadow:0 6px 22px rgba(45,34,27,.05);backdrop-filter:blur(12px)}.workspace-nav-inner{display:flex;align-items:center;gap:12px}.tab-list{display:flex;flex:1;gap:4px;overflow-x:auto;padding:8px 0;scrollbar-width:thin}.tab-list [role="tab"]{display:grid;min-height:44px;place-items:center;padding:0 17px;border:0;border-radius:11px;background:transparent;color:#4f4944;font-size:13px;font-weight:750;white-space:nowrap;cursor:pointer}.tab-list [role="tab"][aria-selected="true"]{background:var(--orange);color:#23140b;box-shadow:inset 0 0 0 1px rgba(139,51,11,.08)}
    .layout{padding:22px 0 52px}.dashboard-panel{min-width:0;padding:clamp(19px,3vw,30px);border:1px solid var(--line);border-radius:24px;background:var(--paper);box-shadow:var(--shadow)}.dashboard-panel[hidden]{display:none}.dashboard-panel:focus{outline:none}.dashboard-panel:focus-visible{outline:3px solid var(--orange-dark);outline-offset:3px}.panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:18px}.panel-head h2{margin:0;font-size:clamp(25px,3.2vw,36px);line-height:1.05;letter-spacing:-.035em}.panel-head p:not(.eyebrow){margin:6px 0 0;color:var(--muted);font-size:13px}.eyebrow{margin:0 0 5px;color:var(--orange-dark);font-size:11px;font-weight:850;letter-spacing:.09em;text-transform:uppercase}.as-of{color:var(--muted);font-size:12px;font-weight:650;white-space:nowrap}
    .overview-hero{padding:18px 24px;border:1px solid var(--orange-soft);border-radius:19px;background:linear-gradient(135deg,#fff1e7,#fff)}.overview-hero>span,.overview-hero>small{display:block;color:#76513a;font-size:12px}.overview-hero>strong{display:block;margin:4px 0 6px;font-size:clamp(36px,5.4vw,56px);font-variant-numeric:tabular-nums;line-height:1;letter-spacing:-.045em;white-space:nowrap}.metric-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:12px}.metric-grid article,.content-card,.activity-totals article{min-width:0;padding:13px 16px;border:1px solid var(--line);border-radius:16px;background:#fff}.metric-grid>article>span{display:block;color:var(--muted);font-size:11px}.metric-grid>article>strong{display:block;margin:5px 0;font-size:clamp(20px,2.3vw,25px);font-variant-numeric:tabular-nums;white-space:nowrap}.return-card{display:grid;grid-template-rows:auto auto 1fr;align-content:start}.return-card .metric-period{font-weight:650;letter-spacing:.01em}.return-card .metric-percent{font-size:clamp(24px,2.8vw,30px);line-height:1.1}.metric-result{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin:8px 0 0;padding-top:8px;border-top:1px solid #ece9e6}.metric-result span{color:var(--muted);font-size:12px}.metric-result strong{margin:0;font-size:14px;line-height:1.3;font-weight:750;font-variant-numeric:tabular-nums;white-space:nowrap}.return-card.is-positive .metric-result strong{color:#315f4b}.return-card.is-negative .metric-result strong{color:#7b4d48}.return-card.is-neutral .metric-result strong{color:#5f5a55}.liquidity-card{display:flex;flex-direction:column;justify-content:center}.overview-grid,.allocation-grid,.activity-totals{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:12px}.content-card h3,.activity-totals h3,.chart-box h3{margin:0 0 10px;font-size:16px}.content-card h4{margin:13px 0 8px;padding-top:10px;border-top:1px solid #ece9e6;font-size:12px}.currency-exposure-inline-note{color:var(--muted);font-weight:400}
    .allocation-row+.allocation-row{margin-top:12px}.allocation-row-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-items:start;gap:16px 26px;width:100%}.allocation-row-grid .allocation-row+.allocation-row{margin-top:0}.allocation-copy{display:flex;justify-content:space-between;gap:14px;margin-bottom:6px;font-size:12px}.allocation-copy span{color:var(--muted);text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}.allocation-track{height:7px;overflow:hidden;border-radius:99px;background:#f0eeec}.allocation-track span{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,var(--orange),#ffc092)}.allocation-row.is-negative .allocation-track span,.currency-exposure-row.is-negative .allocation-track span{background:linear-gradient(90deg,#8f5b57,#c8948e)}.currency-exposure-status{color:#8a5752;font-size:10px;font-weight:650}.allocation-toolbar{width:100%;margin-bottom:14px}.segmented.allocation-toolbar[data-js-inner-controls]{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}.no-js .segmented.allocation-toolbar.no-js-inner-controls{grid-template-columns:repeat(3,minmax(0,1fr))}.allocation-workspace{min-width:0}.allocation-view[hidden]{display:none}.currency-exposure-strip{display:grid;grid-template-columns:180px minmax(0,1fr);align-items:center;gap:18px;margin-top:14px;padding:14px 16px;border:1px solid var(--line);border-radius:16px;background:#fffcfa}.currency-exposure-strip h3{margin:0;font-size:14px}.currency-exposure-single,.currency-exposure-copy{display:flex;align-items:center;gap:8px;font-size:13px}.currency-exposure-single strong,.currency-exposure-copy strong{letter-spacing:.04em}.currency-exposure-single span,.currency-exposure-copy span{color:#9a8e85}.currency-exposure-single b,.currency-exposure-copy b{font-variant-numeric:tabular-nums}.currency-exposure-row+.currency-exposure-row{margin-top:9px}.currency-exposure-copy{justify-content:flex-start;margin-bottom:5px}
    .currency-exposure-heading p{margin:3px 0 0;color:var(--muted);font-size:9.5px}.geography-layout{display:grid;grid-template-columns:minmax(0,2.1fr) minmax(250px,.65fr);align-items:stretch;gap:18px}.world-map-card{min-width:0;margin:0;padding:10px;border:1px solid #eee8e3;border-radius:18px;background:#fff}.world-map{display:block;width:100%;height:auto}.map-ocean{fill:url(#map-ocean-fill);stroke:#eadfd7;stroke-width:1.5}.map-graticule{fill:none;stroke:#eee8e3;stroke-width:1;stroke-dasharray:2 7}.map-land{fill:#dfdcd8;stroke:#fff;stroke-width:1.8;stroke-linejoin:round}.country-marker{cursor:pointer;outline:none;-webkit-tap-highlight-color:transparent}.country-marker-hit{fill:transparent}.country-marker-halo{fill:rgba(255,145,76,.2);stroke:rgba(139,51,11,.18);stroke-width:1;transition:transform .16s ease,fill .16s ease}.country-marker-dot{fill:var(--orange);stroke:#fff;stroke-width:3;filter:drop-shadow(0 3px 4px rgba(93,48,20,.28))}.country-marker-label{fill:#7b3212;font-size:11px;font-weight:850;text-anchor:middle;paint-order:stroke;stroke:#fff;stroke-width:4px;stroke-linejoin:round}.country-marker:hover .country-marker-halo,.country-marker:focus-visible .country-marker-halo,.country-marker[aria-expanded="true"] .country-marker-halo{fill:rgba(255,145,76,.36);transform:scale(1.22)}.country-marker:focus-visible .country-marker-dot{stroke:#7b3212}.world-map-card figcaption{margin:7px 5px 2px;color:var(--muted);font-size:10.5px;line-height:1.35}.geography-side{display:flex;min-width:0;flex-direction:column;gap:10px}.geography-detail-shell{min-height:200px;padding:15px;border:1px solid #f0ded2;border-radius:16px;background:linear-gradient(145deg,#fff7f2,#fff)}.geography-detail-empty{display:grid;min-height:168px;place-items:center;align-content:center;gap:4px;color:var(--muted);text-align:center}.geography-detail-empty[hidden]{display:none}.geography-detail-empty>span{display:grid;width:38px;height:38px;place-items:center;border-radius:50%;background:#fff;color:var(--orange-dark);font-size:19px}.geography-detail-empty strong{color:#3e3834;font-size:13px}.geography-detail-empty small{font-size:10.5px}.geography-detail[hidden]{display:none}.geography-detail>p{margin:0 0 2px;color:var(--orange-dark);font-size:9.5px;font-weight:850;letter-spacing:.08em;text-transform:uppercase}.geography-detail h4{margin:0;font-size:18px}.geography-detail-total{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-top:8px}.geography-detail-total strong{font-size:23px}.geography-detail-total span{font-size:12px;font-weight:800;white-space:nowrap}.geography-detail>small{display:block;color:var(--muted);font-size:9.5px}.geography-detail ul{display:grid;gap:7px;margin:12px 0 0;padding:11px 0 0;border-top:1px solid #efdfd5;list-style:none}.geography-detail li{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:8px;font-size:10.5px}.geography-detail li span,.geography-detail li strong,.geography-detail li small{display:block}.geography-detail li strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.geography-detail li small{color:var(--muted);font-size:8.5px}.geography-detail li b{font-variant-numeric:tabular-nums;white-space:nowrap}.global-instruments-card{display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:center;gap:10px;width:100%;min-height:50px;padding:8px 11px;border:1px solid #e8ded8;border-radius:14px;background:#fff;color:var(--ink);text-align:left;cursor:pointer}.global-instruments-card:hover,.global-instruments-card[aria-expanded="true"]{border-color:#d9a687;background:#fffaf7}.global-icon{display:grid;width:30px;height:30px;place-items:center;border-radius:50%;background:#f4ece7;color:#8b4d2d;font-size:17px}.global-instruments-card strong,.global-instruments-card small{display:block}.global-instruments-card strong{font-size:11px}.global-instruments-card small{color:var(--muted);font-size:9px}.global-instruments-card b{font-size:11px;font-variant-numeric:tabular-nums}.geography-legend{display:grid;gap:2px}.geography-legend-row{display:grid;grid-template-columns:9px minmax(0,1fr) auto;align-items:center;gap:8px;padding:7px 5px;border-bottom:1px solid #eeeae7;font-size:10.5px}.geography-legend-row:last-child{border-bottom:0}.geography-legend-row>span:last-child{color:#4f4944;font-weight:750;font-variant-numeric:tabular-nums;white-space:nowrap}.geography-swatch{width:8px;height:8px;border-radius:50%;background:#b98a6e}.geography-legend-row[data-region-key="usa"] .geography-swatch{background:#ff8f49}.geography-legend-row[data-region-key="canada_latam"] .geography-swatch{background:#efb07c}.geography-legend-row[data-region-key="europe"] .geography-swatch{background:#d8733e}.geography-legend-row[data-region-key="asia_pacific"] .geography-swatch{background:#f39b63}.geography-legend-row[data-region-key="global"] .geography-swatch{background:#8b4d2d}
    .holding-list{border:1px solid var(--line);border-radius:16px;overflow:hidden}.content-card .holding-list{border:0;border-radius:0}.holding-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(86px,.25fr) minmax(120px,.34fr);align-items:center;gap:16px;min-height:52px;padding:8px 14px;border-bottom:1px solid #ece9e6;font-size:12px}.holding-row:last-child{border-bottom:0}.holding-name{min-width:0}.holding-name strong,.holding-name span{display:block}.holding-name span{margin-top:2px;color:var(--muted);font-size:11px}.holding-row>div:not(:first-child){text-align:right;font-variant-numeric:tabular-nums}.holding-header{min-height:42px;background:#fff8f3;color:var(--muted);font-size:10px;font-weight:800;letter-spacing:.04em;text-transform:uppercase}.compact-holdings .holding-row{min-height:45px;padding:6px 0}.compact-holdings .holding-row>div:last-child{white-space:nowrap}
    .performance-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}.segmented{display:flex;gap:3px;padding:3px;border:1px solid var(--line);border-radius:13px;background:#f8f6f4}.segmented button{min-height:44px;padding:0 16px;border:0;border-radius:9px;background:transparent;color:var(--muted);font-size:12px;font-weight:750;cursor:pointer}.segmented label{display:grid;min-height:44px;place-items:center;padding:0 16px;border-radius:9px;color:var(--muted);font-size:12px;font-weight:750;cursor:pointer}.segmented button[aria-pressed="true"]{background:#fff;color:#23140b;box-shadow:0 2px 8px rgba(40,30,24,.09)}.performance-workspace{width:100%;min-width:0}.performance-chart-view{min-width:0;padding:14px;border:1px solid var(--line);border-radius:18px;background:#fff}.performance-chart-view[hidden]{display:none}.chart-view-head{display:flex;align-items:end;justify-content:space-between;gap:18px;padding:2px 8px 8px}.chart-view-head span{color:var(--muted);font-size:11px}.chart-view-head h3{margin:2px 0 0;font-size:19px}.chart-view-head>strong{font-size:24px;font-variant-numeric:tabular-nums;white-space:nowrap}.performance-chart-frame{width:100%;min-width:0}.performance-chart{display:block;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none;touch-action:pan-y}.performance-chart *{-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}.performance-chart-frame svg{width:100%}.performance-chart-frame .performance-chart-desktop{display:block;height:460px}.performance-chart-frame .performance-chart-mobile{display:none;height:auto}.chart-instruction{margin:9px 4px 0;color:var(--muted);font-size:11px}.chart-instruction-mobile{display:none}.chart-grid{stroke:rgba(0,0,0,.075);stroke-dasharray:2 5}.quarter-grid{stroke:rgba(0,0,0,.055);stroke-dasharray:2 6}.zero-line{stroke:rgba(70,70,70,.58);stroke-width:1.25}.axis-text,.quarter-text{fill:#6f6f6f;font-size:11px}.quarter-text{font-weight:700}.performance-area{pointer-events:none}.performance-line{fill:none;stroke:var(--orange);stroke-width:2.15;stroke-linecap:round;stroke-linejoin:miter}.endpoint{fill:#fff;stroke:var(--orange);stroke-width:2}.chart-hit-point{fill:transparent;stroke:none;pointer-events:all}.chart-hit-layer,.chart-live-tooltip{pointer-events:none}.chart-live-tooltip[hidden],.chart-fallback-layer{display:none}.tooltip-box rect{fill:#fff;stroke:rgba(232,111,45,.24)}.tooltip-box text{fill:#5c5c5c;font-size:11px}.tooltip-value{font-weight:700}.tooltip-guide{stroke:rgba(232,111,45,.38)}.tooltip-marker{fill:#fff;stroke:var(--orange);stroke-width:2}
    .activity-totals{margin-bottom:14px}.total-row{display:flex;justify-content:space-between;gap:14px;padding:7px 0;border-bottom:1px solid #ece9e6;font-size:12px}.total-row:last-child{border-bottom:0}.fee-groups{margin:0 0 16px;padding:16px;border:1px solid var(--orange-soft);border-radius:16px;background:#fffaf7}.fee-groups-title,.fee-group-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}.fee-groups-title h3,.fee-group-head h4,.fee-group-grid h5{margin:0}.fee-groups-title p,.fee-group-head small{display:block;margin:3px 0 0;color:var(--muted);font-size:10px}.fee-groups-title label{min-width:220px;font-size:10px;font-weight:750}.fee-groups-title select{display:block;width:100%;min-height:42px;margin-top:4px;padding:0 10px;border:1px solid var(--line);border-radius:10px;background:#fff}.fee-group-panel{margin-top:14px;padding-top:14px;border-top:1px solid #eddfd6}.fee-group-head>strong{font-size:20px;white-space:nowrap}.fee-group-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:12px}.fee-group-grid h5{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}.activity-filters{display:grid;grid-template-columns:minmax(220px,1fr) minmax(180px,.45fr) auto;align-items:end;gap:10px;margin-bottom:12px}.activity-filters label{font-size:11px;font-weight:750}.activity-filters input,.activity-filters select{display:block;width:100%;min-height:44px;margin-top:4px;padding:0 11px;border:1px solid var(--line);border-radius:10px;background:#fff}.result-count{min-width:88px;padding-bottom:12px;color:var(--muted);font-size:11px;text-align:right}.table-scroll{width:100%;max-width:100%;overflow-x:auto;border:1px solid var(--line);border-radius:14px}.activity-table-shell{max-height:510px;overflow:auto}.activity-table{width:100%;min-width:680px;border-collapse:collapse}.activity-table th,.activity-table td{padding:11px 12px;border-bottom:1px solid #ece9e6;font-size:12px;text-align:left}.activity-table thead th{position:sticky;top:0;z-index:1;background:#fff8f3;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.04em}.activity-table .money-cell{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}.activity-empty,.empty-state{margin:0;padding:24px;color:var(--muted);text-align:center}.compact-empty{padding:12px 0;text-align:left}
    .fx-note{margin:16px 0 0;padding-top:12px;border-top:1px solid #eeeae7;color:var(--muted);font-size:11px}.money-cell,.overview-hero>strong,.metric-grid strong,.total-row strong{font-variant-numeric:tabular-nums}button:focus-visible,select:focus-visible,input:focus-visible,.tab-list [role="tab"]:focus,.performance-chart-frame:focus-visible{outline:3px solid var(--orange-dark);outline-offset:2px}
    .dashboard-state{position:fixed;width:1px;height:1px;overflow:hidden;opacity:0;pointer-events:none}.no-js-inner-controls,.geography-no-js-list,.activity-no-js-list{display:none!important}.no-js [data-js-inner-controls],.no-js .geography-layout,.no-js .activity-js-content{display:none!important}.no-js .no-js-inner-controls{display:grid!important}.no-js .geography-no-js-list,.no-js .activity-no-js-list{display:grid!important;gap:9px}.no-js .performance-workspace [data-performance-view],.no-js .allocation-workspace [data-allocation-view]{display:none!important}.geography-no-js-detail,.activity-no-js-group{overflow:hidden;border:1px solid var(--line);border-radius:14px;background:#fff}.geography-no-js-detail summary,.activity-no-js-group summary{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:50px;padding:10px 12px;cursor:pointer;list-style:none}.geography-no-js-detail summary::-webkit-details-marker,.activity-no-js-group summary::-webkit-details-marker{display:none}.geography-no-js-detail summary span,.geography-no-js-detail summary small,.geography-no-js-detail summary strong{display:block}.geography-no-js-detail summary small{color:var(--orange-dark);font-size:9px;font-weight:850;letter-spacing:.07em;text-transform:uppercase}.geography-no-js-body{padding:0 12px 12px;border-top:1px solid #f0ebe7}.geography-no-js-body>small{color:var(--muted);font-size:9.5px}.geography-no-js-body ul{display:grid;gap:7px;margin:10px 0 0;padding:10px 0 0;border-top:1px solid #efdfd5;list-style:none}.geography-no-js-body li{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;font-size:10.5px}.geography-no-js-body li span,.geography-no-js-body li strong,.geography-no-js-body li small{display:block}.geography-no-js-body li small{color:var(--muted);font-size:8.5px}.activity-no-js-group summary b{display:grid;min-width:28px;height:28px;place-items:center;border-radius:50%;background:var(--orange-soft);font-size:11px}.activity-no-js-rows{border-top:1px solid #eeeae7}.activity-no-js-row{display:grid;grid-template-columns:76px minmax(0,1fr) auto;gap:8px;padding:9px 12px;border-bottom:1px solid #f1eeeb;font-size:10.5px}.activity-no-js-row:last-child{border-bottom:0}.activity-no-js-row time{color:var(--muted)}.activity-no-js-row strong{font-variant-numeric:tabular-nums;white-space:nowrap}.no-js-currency-panels{display:none}.no-js .scope-control select{display:none}.no-js .no-js-options{display:flex}.no-js #dashboard-content,.no-js [data-no-js-cell]{display:none}.no-js #dashboard-content .dashboard-panel,.no-js [data-no-js-cell] .dashboard-panel{display:none!important}.no-js #dashboard-tab-overview:checked~.workspace-nav [data-tab="overview"],.no-js #dashboard-tab-performance:checked~.workspace-nav [data-tab="performance"],.no-js #dashboard-tab-portfolio:checked~.workspace-nav [data-tab="portfolio"],.no-js #dashboard-tab-allocation:checked~.workspace-nav [data-tab="allocation"],.no-js #dashboard-tab-activity:checked~.workspace-nav [data-tab="activity"]{background:var(--orange);color:#23140b}.dashboard-ready [data-no-js-cell],.dashboard-ready .no-js-options{display:none!important}
    @media(max-width:760px){.shell{width:min(100% - 20px,1180px)}.header-inner{align-items:flex-start;flex-wrap:wrap}.header-copy h1{font-size:21px}.brand-logo{width:58px;height:48px}.scope-controls{width:100%;flex:0 0 100%;margin-left:0}.scope-control{flex:1}.scope-control select{width:100%;min-width:0}.workspace-nav-inner{width:100%}.tab-list{padding-inline:10px}.layout{padding-top:12px}.dashboard-panel{padding:16px;border-radius:18px}.panel-head{display:block}.as-of{display:block;margin-top:8px;white-space:normal}.metric-grid{grid-template-columns:1fr 1fr}.metric-grid article:last-child{grid-column:1/-1}.overview-grid,.allocation-grid,.activity-totals,.allocation-row-grid{grid-template-columns:1fr}.geography-layout{grid-template-columns:1fr}.world-map-card{padding:6px}.activity-filters{grid-template-columns:1fr 1fr}.result-count{grid-column:1/-1;text-align:left;padding:0}.performance-toolbar{display:grid;grid-template-columns:1fr}.segmented{display:grid;grid-template-columns:1fr 1fr}.performance-chart-view{padding:8px}.performance-chart-frame .performance-chart-desktop{display:none}.performance-chart-frame .performance-chart-mobile{display:block;height:auto;touch-action:none;overscroll-behavior:contain}.chart-view-head>strong{font-size:20px}.chart-instruction-desktop{display:none}.chart-instruction-mobile{display:inline}}
    @media(max-width:520px){.app-header{padding:12px 0 8px}.header-copy p{font-size:11px}.scope-controls{display:grid;grid-template-columns:minmax(0,1fr)}.scope-control small{display:none}.no-js .group-control .no-js-options{display:grid;grid-template-columns:minmax(0,1fr)}.no-js .currency-control .no-js-options{display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr))}.overview-hero{padding:18px}.overview-hero>strong{font-size:36px}.metric-grid{grid-template-columns:1fr}.metric-grid article:last-child{grid-column:auto}.metric-result{margin-top:10px}.holding-row{grid-template-columns:minmax(0,1fr) auto;padding:10px}.holding-header{display:none}.holding-row>div:last-child{grid-column:1/-1;text-align:left}.holding-row>div:last-child:before{content:attr(data-label) ": ";color:var(--muted);font-weight:400}.allocation-copy{display:block}.allocation-copy span{display:block;margin-top:2px;text-align:left}.segmented.allocation-toolbar[data-js-inner-controls],.no-js .segmented.allocation-toolbar.no-js-inner-controls{grid-template-columns:1fr}.currency-exposure-strip{grid-template-columns:1fr}.activity-filters{grid-template-columns:1fr}}
    @media(max-width:390px){.shell{width:calc(100% - 12px)}.header-inner{gap:8px}.brand-logo{width:50px}.header-copy h1{font-size:19px}.scope-control select{padding-inline:8px}.dashboard-panel{padding:13px}.tab-list [role="tab"]{padding-inline:13px}.panel-head h2{font-size:27px}}
    .segmented button[aria-pressed="true"]{color:var(--ink)}
    @page{size:A4 portrait;margin:10mm}
    @media print{html,body{width:190mm;background:#fff}.skip-link,.workspace-nav,.currency-control{display:none!important}.app-header{padding:0 0 3mm}.brand-logo{width:18mm;height:14mm}.header-copy h1{font-size:17pt}.header-copy p{font-size:8pt}.shell{width:100%}.layout{padding:0}.dashboard-panel{border:0;box-shadow:none;padding:0}.dashboard-panel:not(#panel-overview){display:none!important}.panel-head{margin-bottom:3mm}.panel-head h2{font-size:22pt}.overview-hero{padding:4mm 5mm}.overview-hero>strong{font-size:33pt}.metric-grid{grid-template-columns:repeat(3,1fr)!important;gap:3mm;margin-top:3mm}.metric-grid article:last-child{grid-column:auto!important}.metric-grid article,.content-card{padding:3mm}.overview-grid{grid-template-columns:1fr 1fr!important;gap:3mm;margin-top:3mm}.holding-row{min-height:9mm;padding:1mm 0}.fx-note{margin-top:3mm;padding-top:2mm}.overview-grid,.metric-grid,.overview-hero{break-inside:avoid}.activity-filters{display:none!important}}
    """
    style += (
        no_js_currency_display
        + no_js_panel_display
        + no_js_active_currency
        + no_js_active_group
        + no_js_inner_style
    )
    style += """
    /* Stakeholder preview: persistent right-side navigation on desktop. */
    @media(min-width:1101px){body{padding-right:176px}.workspace-nav{position:fixed;z-index:30;top:50%;right:18px;width:150px;transform:translateY(-50%);border:1px solid var(--line);border-radius:18px;background:rgba(255,255,255,.97);box-shadow:var(--shadow)}.workspace-nav-inner{width:100%;padding:8px}.tab-list{display:grid;width:100%;gap:5px;padding:0;overflow:visible}.tab-list [role="tab"]{width:100%;min-height:46px;justify-items:start;padding:0 14px}.layout{padding-top:12px}.dashboard-panel{min-height:0}}
    .header-copy,.panel-head>div,.content-card,.holding-name{overflow-wrap:anywhere}.header-copy h1,.panel-head h2,.content-card h3{word-break:normal}.metric-grid article,.overview-grid>*{min-width:0}
    @media(max-width:1100px){body{padding-right:0}.workspace-nav{position:sticky;top:0;right:auto;width:auto;transform:none;border-inline:0;border-radius:0}.tab-list{display:flex}.tab-list [role="tab"]{width:auto;justify-items:center}}
    @media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition-duration:.01ms!important;animation-duration:.01ms!important;animation-iteration-count:1!important}}
  """
    script = """
    (() => {
      "use strict";
      const allowedTabs = ["overview", "performance", "portfolio", "allocation", "activity"];
      const allowedCurrencies = __ALLOWED_CURRENCIES__;
      const allowedGroups = __ALLOWED_GROUPS__;
      document.querySelectorAll("[data-no-js-cell]").forEach((node) => node.remove());
      const tabs = [...document.querySelectorAll('[role="tab"]')];
      const panels = [...document.querySelectorAll('[role="tabpanel"]')];
      const currencySelect = document.getElementById("reporting-currency");
      const groupSelect = document.getElementById("portfolio-group");
      const performanceState = {metric:"return", period:"all_time"};
      const allocationState = {key:"asset_class"};
      const parseState = () => {
        const params = {};
        location.hash.slice(1).split("&").forEach((part) => {
          const pieces = part.split("=");
          if (pieces.length === 2) params[pieces[0]] = pieces[1];
        });
        return {
          tab: allowedTabs.indexOf(params.tab) >= 0 ? params.tab : "overview",
          group: allowedGroups.indexOf(params.group) >= 0 ? params.group : __DEFAULT_GROUP__,
          currency: allowedCurrencies.indexOf(params.currency) >= 0 ? params.currency : __DEFAULT_CURRENCY__
        };
      };
      let state = parseState();
      const writeHash = () => {
        const nextHash = "#tab=" + state.tab + "&group=" + encodeURIComponent(state.group) + "&currency=" + state.currency;
        try { history.replaceState(null, "", nextHash); }
        catch (error) { location.hash = nextHash; }
      };
      const renderScope = () => {
        panels.forEach((panel) => {
          const template = [...document.querySelectorAll('template[data-group-template][data-mode-template][data-panel-template]')].find((item) => item.dataset.groupTemplate === state.group && item.dataset.modeTemplate === state.currency && item.dataset.panelTemplate === panel.dataset.panel);
          if (template) {
            panel.innerHTML = "";
            panel.appendChild(template.content.cloneNode(true));
          }
        });
        currencySelect.value = state.currency;
        groupSelect.value = state.group;
        document.documentElement.dataset.currency = state.currency;
        document.documentElement.dataset.group = state.group;
        bindActivityFilters();
        bindPerformanceControls();
        bindAllocationControls();
        bindGeographyMaps();
      };
      const activateTab = (key, {focus = false} = {}) => {
        if (allowedTabs.indexOf(key) < 0) key = "overview";
        state.tab = key;
        const stateInput = document.getElementById("dashboard-tab-" + key);
        if (stateInput) stateInput.checked = true;
        let activeTab = null;
        tabs.forEach((tab) => {
          const selected = tab.dataset.tab === key;
          tab.setAttribute("aria-selected", String(selected));
          tab.tabIndex = selected ? 0 : -1;
          if (selected) activeTab = tab;
          if (selected && focus) tab.focus();
        });
        panels.forEach((panel) => { panel.hidden = panel.dataset.panel !== key; });
        if (activeTab) {
          const list = activeTab.closest('.tab-list');
          const left = activeTab.offsetLeft;
          const right = left + activeTab.offsetWidth;
          if (list && (left < list.scrollLeft || right > list.scrollLeft + list.clientWidth)) {
            list.scrollLeft = Math.max(0, left - (list.clientWidth - activeTab.offsetWidth) / 2);
          }
        }
        writeHash();
      };
      const bindActivityFilters = () => {
        document.querySelectorAll('[data-panel="activity"]').forEach((panel) => {
          const controls = [...panel.querySelectorAll("[data-activity-filter]")];
          const rows = [...panel.querySelectorAll(".activity-row")];
          const count = panel.querySelector("[data-activity-count]");
          const empty = panel.querySelector("[data-activity-empty]");
          const apply = () => {
            const values = {};
            controls.forEach((control) => { values[control.dataset.activityFilter] = control.value.trim().toLocaleLowerCase(); });
            let visible = 0;
            rows.forEach((row) => {
              const match = (!values.search || row.dataset.search.includes(values.search)) && (!values.action || row.dataset.action === values.action);
              row.hidden = !match;
              if (match) visible += 1;
            });
            if (count) count.textContent = visible + " из " + rows.length;
            if (empty) empty.hidden = visible !== 0;
          };
          controls.forEach((control) => control.addEventListener(control.tagName === "INPUT" ? "input" : "change", apply));
          apply();
        });
      };
      const bindGeographyMaps = () => {
        document.querySelectorAll('[data-geography-map]').forEach((map) => {
          const targets = [...map.querySelectorAll('[data-geo-target]')];
          const details = [...map.querySelectorAll('[data-geo-detail]')];
          const empty = map.querySelector('[data-geo-empty]');
          let lockedKey = null;
          const show = (key) => {
            let found = false;
            details.forEach((detail) => {
              const selected = detail.dataset.geoDetail === key;
              detail.hidden = !selected;
              if (selected) found = true;
            });
            targets.forEach((target) => {
              target.setAttribute('aria-expanded', String(found && target.dataset.geoTarget === key));
            });
            if (empty) empty.hidden = found;
          };
          const clear = () => {
            details.forEach((detail) => { detail.hidden = true; });
            targets.forEach((target) => target.setAttribute('aria-expanded', 'false'));
            if (empty) empty.hidden = false;
          };
          targets.forEach((target) => {
            const key = target.dataset.geoTarget;
            target.addEventListener('pointerenter', () => show(key));
            target.addEventListener('pointerleave', () => { if (lockedKey === null) clear(); });
            target.addEventListener('focus', () => show(key));
            target.addEventListener('blur', () => { if (lockedKey === null) clear(); });
            target.addEventListener('click', () => {
              if (lockedKey === key) {
                lockedKey = null;
                clear();
              } else {
                lockedKey = key;
                show(key);
              }
            });
            target.addEventListener('keydown', (event) => {
              if ((event.key === 'Enter' || event.key === ' ') && target.tagName !== 'BUTTON') {
                event.preventDefault();
                target.dispatchEvent(new MouseEvent('click', {bubbles:true}));
              }
              if (event.key === 'Escape') {
                lockedKey = null;
                clear();
                target.blur();
              }
            });
          });
          map.addEventListener('pointerleave', () => { if (lockedKey === null) clear(); });
          clear();
        });
      };
      const bindPerformanceControls = () => {
        const panel = document.querySelector('[data-panel="performance"]');
        if (!panel) return;
        const apply = () => {
          panel.querySelectorAll('[data-performance-metric]').forEach((button) => {
            button.setAttribute('aria-pressed', String(button.dataset.performanceMetric === performanceState.metric));
          });
          panel.querySelectorAll('[data-performance-period]').forEach((button) => {
            button.setAttribute('aria-pressed', String(button.dataset.performancePeriod === performanceState.period));
          });
          const key = performanceState.metric + '-' + performanceState.period;
          panel.querySelectorAll('[data-performance-view]').forEach((view) => { view.hidden = view.dataset.performanceView !== key; });
        };
        panel.querySelectorAll('[data-performance-metric]').forEach((button) => button.addEventListener('click', () => {
          performanceState.metric = button.dataset.performanceMetric;
          apply();
        }));
        panel.querySelectorAll('[data-performance-period]').forEach((button) => button.addEventListener('click', () => {
          performanceState.period = button.dataset.performancePeriod;
          apply();
        }));
        const clamp = (value, minimum, maximum) => Math.max(minimum, Math.min(maximum, value));
        panel.querySelectorAll('.performance-chart').forEach((svg) => {
          const focusTarget = svg.closest('.performance-chart-frame') || svg;
          const points = [...svg.querySelectorAll('.chart-hit-point')];
          const tooltip = svg.querySelector('.chart-live-tooltip');
          if (!points.length || !tooltip) return;
          const guide = tooltip.querySelector('.tooltip-guide');
          const marker = tooltip.querySelector('.tooltip-marker');
          const box = tooltip.querySelector('.tooltip-box');
          const dateText = tooltip.querySelector('.tooltip-date');
          const valueText = tooltip.querySelector('.tooltip-value');
          let activeIndex = points.length - 1;
          let activePointerId = null;
          let touchActive = false;
          const isMobileChart = svg.classList.contains('performance-chart-mobile');
          const isVisible = () => getComputedStyle(svg).display !== 'none';
          const hide = () => tooltip.setAttribute('hidden', '');
          const show = (index) => {
            activeIndex = clamp(index, 0, points.length - 1);
            const point = points[activeIndex];
            const pointX = Number(point.dataset.pointX);
            const pointY = Number(point.dataset.pointY);
            const plotLeft = Number(svg.dataset.plotLeft);
            const plotRight = Number(svg.dataset.plotRight);
            const plotTop = Number(svg.dataset.plotTop);
            const plotBottom = Number(svg.dataset.plotBottom);
            const width = Number(tooltip.dataset.tooltipWidth);
            const height = Number(tooltip.dataset.tooltipHeight);
            const boxX = clamp(pointX + width + 12 <= plotRight ? pointX + 12 : pointX - width - 12, plotLeft + 4, plotRight - width - 4);
            const boxY = clamp(pointY - height - 12 >= plotTop ? pointY - height - 12 : pointY + 12, plotTop + 4, plotBottom - height - 4);
            guide.setAttribute('x1', pointX); guide.setAttribute('x2', pointX);
            marker.setAttribute('cx', pointX); marker.setAttribute('cy', pointY);
            box.setAttribute('transform', 'translate(' + boxX + ',' + boxY + ')');
            dateText.textContent = point.dataset.displayDate;
            valueText.textContent = point.dataset.displayValue;
            tooltip.removeAttribute('hidden');
          };
          const showAtClientX = (clientX) => {
            const bounds = svg.getBoundingClientRect();
            if (bounds.width <= 0) return;
            const svgX = ((clientX - bounds.left) / bounds.width) * svg.viewBox.baseVal.width;
            let nearest = 0;
            let distance = Infinity;
            points.forEach((point, index) => {
              const candidate = Math.abs(Number(point.dataset.pointX) - svgX);
              if (candidate < distance) { distance = candidate; nearest = index; }
            });
            show(nearest);
          };
          svg.addEventListener('pointerdown', (event) => {
            if (!isVisible() || (isMobileChart && event.pointerType === 'touch')) return;
            activePointerId = event.pointerId;
            try { svg.setPointerCapture(event.pointerId); } catch (_) {}
            showAtClientX(event.clientX);
          });
          svg.addEventListener('pointermove', (event) => {
            if (!isVisible() || (isMobileChart && event.pointerType === 'touch')) return;
            if (event.pointerType === 'mouse' || activePointerId === event.pointerId) showAtClientX(event.clientX);
          });
          svg.addEventListener('pointerup', (event) => {
            if (isMobileChart && event.pointerType === 'touch') return;
            if (activePointerId !== event.pointerId) return;
            showAtClientX(event.clientX);
            try { svg.releasePointerCapture(event.pointerId); } catch (_) {}
            activePointerId = null;
          });
          svg.addEventListener('pointercancel', () => { activePointerId = null; });
          svg.addEventListener('pointerleave', (event) => {
            if (event.pointerType === 'mouse' && activePointerId === null) hide();
          });
          svg.addEventListener('contextmenu', (event) => event.preventDefault());
          svg.addEventListener('selectstart', (event) => event.preventDefault());
          if (isMobileChart) {
            svg.addEventListener('touchstart', (event) => {
              if (!isVisible() || event.touches.length !== 1) return;
              if (event.cancelable) event.preventDefault();
              touchActive = true;
              showAtClientX(event.touches[0].clientX);
            }, {passive:false, capture:true});
            svg.addEventListener('touchmove', (event) => {
              if (!touchActive || event.touches.length !== 1) return;
              if (event.cancelable) event.preventDefault();
              showAtClientX(event.touches[0].clientX);
            }, {passive:false, capture:true});
            svg.addEventListener('touchend', () => { touchActive = false; }, {passive:false, capture:true});
            svg.addEventListener('touchcancel', () => { touchActive = false; });
          }
          focusTarget.addEventListener('blur', hide);
          focusTarget.addEventListener('keydown', (event) => {
            if (!isVisible()) return;
            if (event.key === 'ArrowLeft' || event.key === 'ArrowRight' || event.key === 'Home' || event.key === 'End') {
              event.preventDefault();
              if (event.key === 'ArrowLeft') show(activeIndex - 1);
              if (event.key === 'ArrowRight') show(activeIndex + 1);
              if (event.key === 'Home') show(0);
              if (event.key === 'End') show(points.length - 1);
            }
            if (event.key === 'Escape') hide();
          });
        });
        apply();
      };
      const bindAllocationControls = () => {
        const panel = document.querySelector('[data-panel="allocation"]');
        if (!panel) return;
        const apply = () => {
          panel.querySelectorAll('[data-allocation-key]').forEach((button) => {
            button.setAttribute('aria-pressed', String(button.dataset.allocationKey === allocationState.key));
          });
          panel.querySelectorAll('[data-allocation-view]').forEach((view) => {
            view.hidden = view.dataset.allocationView !== allocationState.key;
          });
        };
        panel.querySelectorAll('[data-allocation-key]').forEach((button) => button.addEventListener('click', () => {
          allocationState.key = button.dataset.allocationKey;
          apply();
        }));
        apply();
      };
      tabs.forEach((tab, index) => {
        tab.addEventListener("click", () => activateTab(tab.dataset.tab));
        tab.addEventListener("keydown", (event) => {
          let target = null;
          if (event.key === "ArrowRight") target = (index + 1) % tabs.length;
          if (event.key === "ArrowLeft") target = (index - 1 + tabs.length) % tabs.length;
          if (event.key === "Home") target = 0;
          if (event.key === "End") target = tabs.length - 1;
          if (target !== null) { event.preventDefault(); activateTab(tabs[target].dataset.tab, {focus:true}); }
          if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activateTab(tab.dataset.tab); }
        });
      });
      currencySelect.addEventListener("change", () => {
        if (allowedCurrencies.indexOf(currencySelect.value) < 0) return;
        state.currency = currencySelect.value;
        renderScope();
        activateTab(state.tab);
      });
      groupSelect.addEventListener("change", () => {
        if (allowedGroups.indexOf(groupSelect.value) < 0) return;
        state.group = groupSelect.value;
        renderScope();
        activateTab(state.tab);
      });
      addEventListener("hashchange", () => {
        state = parseState();
        renderScope();
        activateTab(state.tab);
      });
      renderScope();
      activateTab(state.tab);
      document.documentElement.classList.remove("no-js");
      document.documentElement.classList.add("dashboard-ready");
      document.documentElement.setAttribute("data-dashboard-ready", "true");
    })();
    """
    script = script.replace(
        "__ALLOWED_CURRENCIES__", json.dumps(available, ensure_ascii=True)
    ).replace("__ALLOWED_GROUPS__", json.dumps(group_ids, ensure_ascii=True)).replace(
        "__DEFAULT_GROUP__", json.dumps(default_group)
    ).replace("__DEFAULT_CURRENCY__", json.dumps(default_currency))
    return f"""<!DOCTYPE html>
<html lang="ru" class="no-js" data-currency="{escape(default_currency, quote=True)}" data-group="{escape(default_group, quote=True)}">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <meta name="report-date" content="{escape(report_date, quote=True)}"/>
  <meta name="reporting-currency" content="{escape(default_currency, quote=True)}"/>
  <meta name="renderer-version" content="{RENDERER_VERSION}"/>
  <title>Ключевые показатели портфеля</title>
  <style>{style}</style>
</head>
<body data-renderer-version="{RENDERER_VERSION}">
  {state_inputs}
  <a class="skip-link" href="#panel-overview">К содержанию</a>
  <header class="app-header"><div class="header-inner shell">
    <img src="{escape(logo, quote=True)}" class="brand-logo" alt="Portfolio demo"/>
    <div class="header-copy"><h1>Ключевые показатели<br/>портфеля</h1><p>Отчёт на {escape(display_date(report_date))}</p></div>
    <div class="scope-controls">
      <div class="scope-control group-control"><label for="portfolio-group">Группа портфеля</label><small>выбирает состав</small><select id="portfolio-group" aria-label="Группа портфеля">{group_options}</select><div class="no-js-options" aria-label="Группа портфеля">{no_js_group_options}</div></div>
      <div class="scope-control currency-control"><label for="reporting-currency">Валюта отчёта</label><small>меняет отображение</small><select id="reporting-currency" aria-label="Валюта отчёта">{currency_options}</select><div class="no-js-options" aria-label="Валюта отчёта">{no_js_currency_options}</div></div>
    </div>
  </div></header>
  <nav class="workspace-nav" aria-label="Разделы отчёта"><div class="workspace-nav-inner shell"><div class="tab-list" role="tablist" aria-label="Разделы портфеля">{tabs}</div></div></nav>
  <main id="dashboard-content" class="layout shell">{initial_panels}</main>
  {fallback_mains}
  {templates}
  <script type="application/json" id="client-view-data">{client_views_embedded}</script>
  <script type="application/json" id="detailed-report-data">{embedded}</script>
  <script>{script}</script>
  <!-- Local sources: {escape(report_source.name)}, {escape(series_source.name)}, {escape(detailed_source.name)} -->
</body>
</html>
"""


def render_detailed_dashboard(
    report: dict[str, Any],
    points: list[SeriesPoint],
    detailed: dict[str, Any],
    logo: str,
    report_source: Path,
    series_source: Path,
    detailed_source: Path,
    *,
    client_views: Mapping[str, Any] | None = None,
) -> str:
    errors = validate_detailed_report(detailed)
    if errors:
        raise DashboardError("detailed report schema validation failed: " + "; ".join(errors))
    if client_views is not None:
        return render_client_workspace(
            report,
            detailed,
            logo,
            report_source,
            series_source,
            detailed_source,
            client_views,
        )
    sections = detailed["sections"]
    currency = required_text(report, "reporting_currency").upper()
    report_date = required_text(report, "report_date")
    all_time = report["periods"]["all_time"]
    current_year = report["periods"]["current_year"]
    chart_points = downsample_points(points)
    return_chart = render_chart(chart_points, metric="return", currency=currency)
    market_chart = render_chart(chart_points, metric="market", currency=currency)

    navigation = (
        ("summary", "Сводка"),
        ("performance", "Доходность"),
        ("holdings", "Позиции"),
        ("currencies", "Валюты"),
        ("allocation", "Структура"),
        ("transactions", "Операции"),
        ("income", "Доходы"),
        ("fees_taxes", "Комиссии и налоги"),
        ("accounts", "Счета"),
    )
    nav_links = "".join(
        f'<a href="#section-{key}" data-nav-key="{key}"'
        + (' aria-current="page"' if key == "summary" else "")
        + f'>{escape(label)}</a>'
        for key, label in navigation
    )
    mobile_options = "".join(
        f'<option value="section-{key}">{escape(label)}</option>'
        for key, label in navigation
    )

    metric_rows = "".join(
        "<tr>"
        f'<th scope="row">{escape(label)}</th>'
        f'<td id="metric-all-time-{id_key}" data-source-field="periods.all_time.{key}">{escape(display(all_time[key]))}</td>'
        f'<td id="metric-current-year-{id_key}" data-source-field="periods.current_year.{key}">{escape(display(current_year[key]))}</td>'
        "</tr>"
        for key, id_key, label, display in (
            ("cumulative_ttwror", "cumulative", "Доходность", lambda value: format_percent(_detail_decimal(value, "all-time return"))),
            ("annualized_ttwror", "annualized", "Доходность в годовом выражении", lambda value: format_percent(_detail_decimal(value, "annualized return"))),
            ("irr", "irr", "С учётом пополнений и выводов", lambda value: format_percent(_detail_decimal(value, "IRR"))),
            ("profit", "profit", "Прибыль", lambda value: format_money(_detail_decimal(value, "profit"), currency)),
        )
    )

    overview = path_value(report, "portfolio_overview")
    overview_cash = overview["cash"]
    overview_allocation = overview["allocation_by_asset_class"]
    overview_holdings = overview["top_holdings"]
    summary_allocation_rows = "".join(
        '<div class="overview-allocation-row">'
        '<div class="overview-allocation-copy">'
        f'<strong>{escape(text_value(item["name"], "allocation name"))}</strong>'
        f'<span>{escape(format_percent(decimal_value(item["weight"], "allocation weight")))} · '
        f'{escape(format_money(decimal_value(item["market_value"], "allocation market value"), currency))}</span>'
        '</div><div class="allocation-track" aria-hidden="true">'
        f'<span style="width:{css_bar_percent(decimal_value(item["weight"], "allocation weight"))}%"></span>'
        '</div></div>'
        for item in overview_allocation
    )
    summary_allocation_rows += (
        '<div class="overview-allocation-row">'
        '<div class="overview-allocation-copy"><strong>Денежные средства</strong>'
        f'<span>{escape(format_percent(decimal_value(overview_cash["weight"], "cash weight")))} · '
        f'{escape(format_money(decimal_value(overview_cash["market_value"], "cash market value"), currency))}</span>'
        '</div><div class="allocation-track" aria-hidden="true">'
        f'<span style="width:{css_bar_percent(decimal_value(overview_cash["weight"], "cash weight"))}%"></span>'
        '</div></div>'
    )
    summary_holding_rows = "".join(
        '<div class="overview-holding-row" role="row">'
        '<div class="overview-holding-name" role="cell">'
        f'<strong>{escape(text_value(item["name"], "holding name"))}</strong>'
        f'<span>{escape(text_value(item["asset_class"], "holding asset class"))} · '
        f'{escape(text_value(item["currency"], "holding currency"))}</span></div>'
        f'<div role="cell" data-label="Доля">{escape(format_percent(decimal_value(item["weight"], "holding weight")))}</div>'
        f'<div role="cell" data-label="Стоимость"><strong>{escape(format_money(decimal_value(item["market_value"], "holding market value"), currency))}</strong></div>'
        '</div>'
        for item in overview_holdings
    ) or '<p class="empty">Нет открытых позиций</p>'

    holding_rows = "".join(
        "<tr "
        f'id="{escape(row["id"], quote=True)}" '
        f'data-security-currency="{escape(row["currency"], quote=True)}" '
        f'data-reporting-currency="{escape(currency, quote=True)}">'
        f'<th scope="row"><strong>{escape(row["name"])}</strong></th>'
        f'<td>{escape(row["portfolio_name"])}</td>'
        f'<td>{escape(row["quantity"])}</td>'
        f'<td>{escape(_money_text(row["price"], row["currency"])) if row["price"] is not None else "Нет цены"}'
        + "</td>"
        f'<td><strong>{escape(_money_text(row["market_value"], row["currency"])) if row["market_value"] is not None else "Нет данных"}</strong></td>'
        f'<td>{escape(row["currency"])}</td>'
        "</tr>"
        for row in sections["holdings"]["rows"]
    ) or '<tr><td colspan="6" class="empty">Нет текущих позиций</td></tr>'

    currency_panels = "".join(
        '<article class="currency-panel" '
        f'data-currency="{escape(code, quote=True)}">'
        f'<div class="currency-card-head"><h3>{escape(code)}</h3></div>'
        f'<dl><div><dt>Стоимость бумаг</dt><dd>{escape(_money_text(item["market_value"], code))}</dd></div>'
        f'<div><dt>Остаток на счетах</dt><dd>{escape(_money_text(item["cash_balance"], code))}</dd></div>'
        f'<div class="currency-total"><dt>Итого в этой валюте</dt><dd>{escape(_money_text(item["total_value"], code))}</dd></div></dl>'
        "</article>"
        for code, item in sorted(sections["currencies"]["per_currency"].items())
    )
    consolidated = sections["currencies"]["consolidated"]
    if consolidated["complete_value"] is None:
        consolidated_markup = _client_notice(
            "Общая сумма по всем валютам не показана: в исходном файле нет курса "
            "для корректного пересчёта на дату отчёта. Поэтому суммы выше приведены "
            "отдельно в каждой валюте.",
            tone="important",
        )
    else:
        consolidated_markup = (
            '<article class="currency-panel consolidated-panel">'
            '<div class="currency-card-head"><h3>Итого по портфелю</h3>'
            f'<span>в валюте отчёта · {escape(consolidated["reporting_currency"])}</span></div>'
            f'<strong class="consolidated-value">{escape(_money_text(consolidated["complete_value"], consolidated["reporting_currency"]))}</strong>'
            "</article>"
        )

    taxonomy_options = "".join(
        f'<option value="taxonomy-panel-{index}">{escape(item["name"])}</option>'
        for index, item in enumerate(sections["allocation"]["taxonomies"])
    )
    taxonomy_panels = "".join(
        f'<article id="taxonomy-panel-{index}" class="taxonomy-panel"'
        + ("" if index == 0 else " hidden")
        + f' data-taxonomy-id="{escape(item["taxonomy_id"], quote=True)}">'
        f'<h3>{escape(item["name"])}</h3>'
        + "".join(
            _render_taxonomy_node(child)
            for child in item["root"]["children"]
        )
        + "</article>"
        for index, item in enumerate(sections["allocation"]["taxonomies"])
    ) or '<p class="empty">Таксономии не заданы</p>'

    transaction_types = sorted({row["type"] for row in sections["transactions"]["rows"]})
    transaction_currencies = sorted(
        {
            item["currency"]
            for row in sections["transactions"]["rows"]
            for item in row["cash_impacts"] + row["units"]
        }
    )
    account_lookup = {
        row["id"]: (
            f'Начисленные проценты · {row["currency"]}'
            if row["technical"]["is_technical"]
            else row["name"]
        )
        for row in sections["accounts"]["rows"]
    }
    type_options = "".join(
        f'<option value="{escape(item, quote=True)}">{escape(_transaction_label(item, item))}</option>'
        for item in transaction_types
    )
    currency_options = "".join(f'<option value="{escape(item, quote=True)}">{escape(item)}</option>' for item in transaction_currencies)
    transaction_rows = "".join(
        '<tr class="transaction-row" '
        f'id="{escape(row["id"], quote=True)}" '
        f'data-type="{escape(row["type"], quote=True)}" '
        f'data-currencies="{escape(" ".join(sorted({item["currency"] for item in row["cash_impacts"] + row["units"]})), quote=True)}" '
        f'data-accounts="{escape(" ".join(row["account_ids"]), quote=True)}" '
        f'data-search="{escape(_transaction_label(row["type"], row["label"]).casefold(), quote=True)}">'
        f'<td>{escape(display_date(row["date"]))}</td>'
        f'<th scope="row">{escape(_transaction_label(row["type"], row["label"]))}</th>'
        '<td>' + "<br/>".join(
            f'{escape(_money_text(item["amount"], item["currency"]))} ({escape(item["currency"])})'
            for item in row["cash_impacts"]
        ) + ("—" if not row["cash_impacts"] else "") + "</td>"
        f'<td>{escape(", ".join(account_lookup.get(item, item) for item in row["account_ids"])) or "—"}</td>'
        "</tr>"
        for row in sections["transactions"]["rows"]
    )

    def bucket_table(
        section: Mapping[str, Any],
        labels: Mapping[str, str],
        *,
        hide_zero_rows: bool = False,
    ) -> str:
        codes = sorted(section["by_currency"])
        head = "".join(f'<th scope="col">{escape(code)}</th>' for code in codes)
        visible_labels = {
            key: label
            for key, label in labels.items()
            if not hide_zero_rows
            or any(
                _detail_decimal(
                    section["by_currency"][code].get(key, "0"),
                    f"{key} in {code}",
                ) != 0
                for code in codes
            )
        }
        body = "".join(
            f'<tr><th scope="row">{escape(label)}</th>'
            + "".join(
                f'<td>{escape(_money_text(section["by_currency"][code].get(key, "0"), code))}</td>'
                for code in codes
            )
            + "</tr>"
            for key, label in visible_labels.items()
        )
        if not body:
            return '<p class="empty-state">За отчётный период таких операций не было.</p>'
        return f'<div class="table-scroll"><table><thead><tr><th>Категория</th>{head}</tr></thead><tbody>{body}</tbody></table></div>'

    income_table = bucket_table(
        sections["income"],
        {
            "dividends": "Дивиденды",
            "realized_interest": "Полученный процентный доход",
            "accrued_aci": "Начисленные, но ещё не выплаченные проценты",
        },
        hide_zero_rows=True,
    )
    charges_table = bucket_table(
        sections["fees_taxes"],
        {
            "fees": "Комиссии",
            "taxes": "Налоги",
            "interest_charges": "Процентные списания",
            "other_charges": "Прочие списания",
        },
    )

    client_accounts = [
        row
        for row in sections["accounts"]["rows"]
        if not row["technical"]["is_technical"]
    ]
    account_rows = "".join(
        f'<tr id="{escape(row["id"], quote=True)}" data-account-currency="{escape(row["currency"], quote=True)}" data-reporting-currency="{escape(currency, quote=True)}">'
        f'<th scope="row"><strong>{escape(row["name"])}</strong></th>'
        f'<td><strong>{escape(_money_text(row["balance"], row["currency"]))}</strong></td>'
        f'<td>{escape(row["currency"])}</td></tr>'
        for row in client_accounts
    ) or '<tr><td colspan="3" class="empty">Нет денежных счетов</td></tr>'

    embedded = script_safe_json(
        {
            "presentation_contract": "1.0",
            "schema_version": detailed["schema_version"],
            "report_date": report_date,
            "reporting_currency": currency,
            "source_sha256": detailed["metadata"]["source_sha256"],
            "financial_status": sections["currencies"]["consolidated"]["status"],
            "section_counts": {
                "holdings": len(sections["holdings"]["rows"]),
                "transactions": len(sections["transactions"]["rows"]),
                "accounts": len(sections["accounts"]["rows"]),
                "securities": len(sections["securities"]["rows"]),
                "taxonomies": len(sections["allocation"]["taxonomies"]),
            },
        }
    )
    client_views_embedded = script_safe_json(client_views or {})
    style = """
    :root{--orange:#ff914c;--orange-dark:#a8420f;--orange-soft:#ffdfca;--peach:#fff7f2;--paper:#fff;--ink:#111;--muted:#69645f;--line:#e2e6e9;--shadow:0 12px 34px rgba(55,34,20,.07)}
    *{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;min-width:320px;overflow-x:hidden;background:linear-gradient(180deg,#fffefe 0%,#fff8f3 100%);color:var(--ink);font-family:"Montserrat","Avenir Next",Avenir,Arial,sans-serif;line-height:1.45;-webkit-text-size-adjust:100%}
    a{color:inherit}.skip-link{position:absolute;left:-9999px;top:8px}.skip-link:focus{left:8px;z-index:30;background:#fff;padding:12px;border:2px solid var(--orange-dark);border-radius:10px}
    .app-header{background:transparent}.header-inner,.layout{width:min(1120px,calc(100% - 32px));margin:auto}.header-inner{display:flex;align-items:center;gap:18px;padding:22px 4px 16px}.brand-logo{display:block;width:78px;height:62px;object-fit:contain;flex:0 0 auto}.header-copy{min-width:0}.header-inner h1{margin:0;font-size:clamp(24px,3vw,34px);line-height:1.03;letter-spacing:-.035em}.header-inner p{margin:6px 0 0;color:var(--muted);font-size:13px}
    .dashboard-nav{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.96);border-block:1px solid rgba(226,230,233,.9);box-shadow:0 5px 18px rgba(45,34,27,.045);backdrop-filter:blur(12px)}.desktop-nav{display:flex;gap:5px;overflow-x:auto;padding:8px max(16px,calc((100vw - 1120px)/2));scrollbar-width:thin}.desktop-nav a{min-height:44px;display:grid;place-items:center;padding:0 13px;border-radius:10px;text-decoration:none;white-space:nowrap;font-size:12px;font-weight:700;color:#4d4844}.desktop-nav a[aria-current="page"]{background:var(--orange);color:#24150c}.mobile-nav{display:none;padding:8px 12px}.mobile-nav label{font-size:12px;font-weight:700}.mobile-nav select{width:100%;min-height:44px;margin-top:4px;border:1px solid var(--line);border-radius:10px;background:#fff;padding:0 12px;font:inherit}
    .layout{padding:26px 0 58px}.dashboard-section{scroll-margin-top:82px;margin:0 0 22px;padding:clamp(20px,3vw,30px);min-width:0;background:var(--paper);border:1px solid var(--line);border-radius:24px;box-shadow:var(--shadow)}.dashboard-section:focus{outline:none}.dashboard-section:focus-visible{outline:2px solid var(--orange);outline-offset:2px}.section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:18px}.section-head h2{margin:0;font-size:clamp(21px,2.5vw,27px);line-height:1.15;letter-spacing:-.025em}.section-head p{max-width:720px;margin:6px 0 0;color:var(--muted);font-size:13px}.count{flex:0 0 auto;color:var(--muted);font-size:12px;font-weight:600}
    .hero-metric{display:flex;align-items:end;justify-content:space-between;gap:18px;padding:21px 22px;border:1px solid var(--orange-soft);border-radius:17px;background:linear-gradient(135deg,rgba(255,223,202,.55),#fff)}.hero-metric strong{display:block;font-size:clamp(31px,5vw,47px);line-height:1;letter-spacing:-.04em}.hero-metric span{display:block;margin-bottom:5px;color:#77553f;font-size:12px;font-weight:600}.hero-metric>div:last-child{color:var(--muted);font-size:12px}.hero-metric>div:last-child strong{display:inline;font-size:inherit;letter-spacing:0}
    .summary-overview-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:18px}.summary-overview-card{min-width:0;padding:18px;border:1px solid var(--line);border-radius:16px}.summary-overview-card h3{margin:0 0 14px;font-size:17px}.overview-allocation-row+.overview-allocation-row{margin-top:12px}.overview-allocation-copy{display:flex;justify-content:space-between;gap:12px;margin-bottom:6px;font-size:12px}.overview-allocation-copy span{color:var(--muted);text-align:right}.allocation-track{height:6px;overflow:hidden;border-radius:999px;background:#f0f0f0}.allocation-track span{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,var(--orange),#ffc092)}.overview-holdings{display:grid}.overview-holding-row{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:14px;align-items:center;padding:9px 0;border-bottom:1px solid #ececec;font-size:12px}.overview-holding-row:last-child{border-bottom:0}.overview-holding-name{min-width:0}.overview-holding-name strong,.overview-holding-name span{display:block}.overview-holding-name span,.subline{margin-top:2px;color:var(--muted);font-size:11px;font-weight:400}
    .table-scroll{width:100%;max-width:100%;overflow-x:auto;border:1px solid var(--line);border-radius:14px;background:#fff}table{width:100%;border-collapse:collapse;min-width:700px}th,td{padding:12px 14px;border-bottom:1px solid #e7e7e7;text-align:left;vertical-align:top;font-size:12px}thead th{background:#fff7f2;color:#6d4a34;font-size:11px;text-transform:uppercase;letter-spacing:.045em}tbody th{font-weight:600}tbody tr:last-child>*{border-bottom:0}.metrics{margin-top:18px}.metrics thead th:not(:first-child){background:var(--orange);color:#fff;text-align:center}.metrics td{font-size:15px;font-weight:600;text-align:center}.metrics tbody th{font-size:12px}.subline{display:block}
    .chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.chart-box{min-width:0;overflow:hidden;border:1px solid var(--line);border-radius:16px;padding:14px;background:linear-gradient(180deg,rgba(255,223,202,.13),#fff 38%)}.chart-box h3{margin:0 0 8px;font-size:16px}.performance-chart{display:block;width:100%;height:auto;font-family:"Montserrat","Avenir Next",Avenir,Arial,sans-serif}.grid{stroke:rgba(0,0,0,.075);stroke-dasharray:2 5}.quarter-grid{stroke:rgba(0,0,0,.055);stroke-dasharray:2 6}.zero-line{stroke:rgba(70,70,70,.58);stroke-width:1.25}.axis-text,.quarter-text{fill:#6f6f6f;font-size:11px}.quarter-text{font-weight:700}.performance-area{pointer-events:none}.performance-line{fill:none;stroke:var(--orange);stroke-width:2.15;stroke-linecap:round;stroke-linejoin:miter}.endpoint{fill:#fff;stroke:var(--orange);stroke-width:2}.chart-hit-point{fill:none;stroke:none;pointer-events:all}.chart-hit-layer,.chart-live-tooltip{pointer-events:none}.chart-live-tooltip[hidden]{display:none}.tooltip-box rect{fill:#fff;stroke:rgba(232,111,45,.24)}.tooltip-box text{fill:#5c5c5c;font-size:11px}.tooltip-value{font-weight:700}.tooltip-guide{stroke:rgba(232,111,45,.38)}.tooltip-marker{fill:#fff;stroke:var(--orange);stroke-width:2}
    .currency-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(280px,100%),1fr));gap:14px}.currency-panel{min-width:0;padding:18px;border:1px solid var(--line);border-radius:16px;background:#fff}.currency-card-head{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-bottom:12px}.currency-card-head h3{margin:0;font-size:22px}.currency-card-head span{color:var(--muted);font-size:11px;text-align:right}.currency-panel dl{margin:0}.currency-panel dl div{display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px solid #ededed;font-size:12px}.currency-panel dd{margin:0;font-weight:650;text-align:right}.currency-panel .currency-total{padding-top:12px;border-bottom:0}.currency-total dd{font-size:16px}.consolidated-panel{border-color:var(--orange-soft);background:var(--peach)}.consolidated-value{font-size:27px}.client-notice{grid-column:1/-1;margin:0;padding:15px 17px;border-radius:13px;background:#f7f5f3;color:#554f4a;font-size:13px}.client-notice-important{border:1px solid var(--orange-soft);background:#fff8f3}
    .taxonomy-toolbar,.filters{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:16px}.taxonomy-toolbar{grid-template-columns:minmax(220px,420px)}label.control{font-size:12px;font-weight:700}label.control input,label.control select{display:block;width:100%;min-height:44px;margin-top:4px;border:1px solid var(--line);border-radius:10px;background:#fff;padding:0 11px;font:inherit}.taxonomy-panel>h3{margin:0 0 10px}.taxonomy-node{margin:8px 0 8px 12px;border-left:2px solid var(--orange-soft);padding-left:12px}.taxonomy-node>summary,.taxonomy-leaf{min-height:44px;cursor:pointer;display:flex;align-items:center;justify-content:space-between;gap:12px}.taxonomy-node small{color:var(--muted);text-align:right}
    .empty,.empty-state{padding:24px;text-align:center;color:var(--muted)}.compact-table table{min-width:520px}.accounts-table table{min-width:420px}button,a,select,input,summary{touch-action:manipulation}details>summary{min-height:44px}button:focus-visible,a:focus-visible,select:focus-visible,input:focus-visible,summary:focus-visible{outline:3px solid var(--orange-dark);outline-offset:2px}
    @media(max-width:900px){.chart-grid,.summary-overview-grid{grid-template-columns:1fr}.filters{grid-template-columns:1fr 1fr}.header-inner,.layout{width:min(100% - 20px,1120px)}}
    @media(max-width:720px){.desktop-nav{display:none}.mobile-nav{display:block}.layout{padding-top:12px}.dashboard-section{scroll-margin-top:76px;padding:16px;border-radius:18px}.section-head,.hero-metric{display:block}.hero-metric>div+div{margin-top:12px}.filters{grid-template-columns:1fr}.taxonomy-node{margin-left:2px}.taxonomy-node>summary,.taxonomy-leaf{display:block;padding:10px 0}.taxonomy-node small{display:block;text-align:left}.chart-box{padding:8px}.performance-chart-desktop{display:block}table{min-width:660px}.metrics table{display:block;min-width:0}.metrics thead{display:none}.metrics tbody{display:block}.metrics tr{display:grid;grid-template-columns:1fr 1fr}.metrics tbody th{grid-column:1/-1;padding:12px 12px 6px;border-bottom:0;background:#fff8f3}.metrics td{padding:7px 12px 12px;border-bottom:1px solid #e7e7e7;text-align:left}.metrics td:before{display:block;margin-bottom:3px;color:var(--muted);font-size:10px;font-weight:500;text-transform:uppercase;letter-spacing:.03em}.metrics td:nth-of-type(1):before{content:"За всё время"}.metrics td:nth-of-type(2):before{content:"За текущий год"}.summary-overview-card{padding:15px}}
    @media(max-width:520px){.header-inner{align-items:flex-start;gap:10px;padding:15px 2px 12px}.brand-logo{width:58px;height:48px}.header-inner h1{font-size:21px}.hero-metric strong{font-size:34px}.overview-holding-row{grid-template-columns:minmax(0,1fr) auto}.overview-holding-row>div:last-child{grid-column:1/-1}.overview-holding-row>div:last-child:before{content:attr(data-label) ": ";color:var(--muted);font-weight:400}.section-head h2{font-size:22px}.currency-card-head{display:block}.currency-card-head span{display:block;margin-top:3px;text-align:left}}
    @media(max-width:390px){.layout{width:calc(100% - 12px)}.dashboard-section{padding:13px}.hero-metric{padding:17px}.hero-metric strong{font-size:31px}.metrics{margin-inline:-1px}.section-head p{font-size:12px}}
    @media print{.dashboard-nav,.filters,.taxonomy-toolbar{display:none}.layout{width:100%;padding:0}.dashboard-section{box-shadow:none;break-inside:avoid}}
    """
    script = """
    (() => {
      "use strict";
      const sections = [...document.querySelectorAll("[data-dashboard-section]")];
      const links = [...document.querySelectorAll("[data-nav-key]")];
      const mobileNav = document.getElementById("mobile-section-nav");
      const setActive = (key) => {
        links.forEach((link) => {
          if (link.dataset.navKey === key) link.setAttribute("aria-current", "page");
          else link.removeAttribute("aria-current");
        });
        if (mobileNav) mobileNav.value = "section-" + key;
      };
      links.forEach((link) => link.addEventListener("click", () => setActive(link.dataset.navKey)));
      mobileNav?.addEventListener("change", () => {
        const target = document.getElementById(mobileNav.value);
        if (target) { location.hash = mobileNav.value; target.focus({preventScroll:true}); target.scrollIntoView(); }
      });
      let scrollTicking = false;
      const syncActiveSection = () => {
        scrollTicking = false;
        if (window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 4) {
          const hashTarget = location.hash
            ? document.getElementById(location.hash.slice(1))
            : null;
          const last = hashTarget?.matches("[data-dashboard-section]")
            ? hashTarget
            : sections.at(-1);
          if (last) setActive(last.dataset.dashboardSection);
          return;
        }
        let current = sections[0];
        for (const section of sections) {
          if (section.getBoundingClientRect().top <= 118) current = section;
          else break;
        }
        if (current) setActive(current.dataset.dashboardSection);
      };
      addEventListener("scroll", () => {
        if (scrollTicking) return;
        scrollTicking = true;
        requestAnimationFrame(syncActiveSection);
      }, {passive:true});
      addEventListener("hashchange", () => requestAnimationFrame(syncActiveSection));
      requestAnimationFrame(syncActiveSection);
      const taxonomySelect = document.getElementById("taxonomy-select");
      taxonomySelect?.addEventListener("change", () => {
        document.querySelectorAll(".taxonomy-panel").forEach((panel) => { panel.hidden = panel.id !== taxonomySelect.value; });
      });
      const filterControls = [...document.querySelectorAll("[data-transaction-filter]")];
      const transactionRows = [...document.querySelectorAll(".transaction-row")];
      const count = document.getElementById("transaction-count");
      const applyFilters = () => {
        const values = Object.fromEntries(filterControls.map((control) => [control.dataset.transactionFilter, control.value.trim().toLocaleLowerCase()]));
        let visible = 0;
        transactionRows.forEach((row) => {
          const match = (!values.search || row.dataset.search.includes(values.search)) &&
            (!values.type || row.dataset.type.toLocaleLowerCase() === values.type) &&
            (!values.currency || row.dataset.currencies.toLocaleLowerCase().split(" ").includes(values.currency)) &&
            (!values.account || row.dataset.accounts.split(" ").includes(values.account));
          row.hidden = !match;
          if (match) visible += 1;
        });
        if (count) count.textContent = visible + " из " + transactionRows.length;
      };
      filterControls.forEach((control) => control.addEventListener(control.tagName === "INPUT" ? "input" : "change", applyFilters));
      applyFilters();
    })();
    """
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <meta name="report-date" content="{escape(report_date, quote=True)}"/>
  <meta name="reporting-currency" content="{escape(currency, quote=True)}"/>
  <meta name="calculation-engine" content="{escape(required_text(report, "calculation_engine.source"), quote=True)}"/>
  <meta name="calculation-engine-commit" content="{escape(required_text(report, "calculation_engine.version_or_commit"), quote=True)}"/>
  <meta name="renderer-version" content="{RENDERER_VERSION}"/>
  <meta name="chart-source-points" content="{len(points)}"/>
  <meta name="chart-rendered-points" content="{len(chart_points)}"/>
  <title>Ключевые показатели портфеля</title>
  <style>{style}</style>
</head>
<body data-renderer-version="{RENDERER_VERSION}">
  <a class="skip-link" href="#section-summary">К содержанию</a>
  <header class="app-header"><div class="header-inner">
    <img src="{escape(logo, quote=True)}" class="brand-logo" alt="Portfolio demo"/>
    <div class="header-copy"><h1>Ключевые показатели<br/>портфеля</h1><p>Отчёт на {escape(display_date(report_date))} · {escape(currency)}</p></div>
  </div></header>
  <nav class="dashboard-nav" aria-label="Разделы отчёта">
    <div class="desktop-nav">{nav_links}</div>
    <div class="mobile-nav"><label for="mobile-section-nav">Раздел</label><select id="mobile-section-nav">{mobile_options}</select></div>
  </nav>
  <main id="dashboard-content" class="layout">
    <section id="section-summary" class="dashboard-section" data-dashboard-section="summary" tabindex="-1" aria-labelledby="heading-summary">
      <div class="section-head"><div><h2 id="heading-summary">Главное</h2><p>Стоимость портфеля и ключевые показатели доходности</p></div></div>
      <div class="hero-metric"><div><span>Рыночная стоимость портфеля</span><strong id="portfolio-market-value" data-source-field="portfolio_market_value">{escape(format_money(required_decimal(report, "portfolio_market_value"), currency))}</strong></div><div>Валюта отчёта: <strong>{escape(currency)}</strong></div></div>
      <div class="table-scroll metrics"><table aria-label="Показатели эффективности портфеля"><thead><tr><th>Показатель</th><th>За всё время</th><th>За текущий год</th></tr></thead><tbody>{metric_rows}</tbody></table></div>
      <div class="summary-overview-grid">
        <article class="summary-overview-card"><h3>Структура портфеля</h3>{summary_allocation_rows}</article>
        <article class="summary-overview-card"><h3>Крупнейшие позиции</h3><div class="overview-holdings" role="table" aria-label="Крупнейшие позиции">{summary_holding_rows}</div></article>
      </div>
    </section>
    <section id="section-performance" class="dashboard-section" data-dashboard-section="performance" tabindex="-1" aria-labelledby="heading-performance">
      <div class="section-head"><div><h2 id="heading-performance">Доходность</h2><p>Динамика результата и стоимости портфеля за отчётный период</p></div></div>
      <div class="chart-grid"><article class="chart-box"><h3>Рост портфеля, %</h3>{return_chart}</article><article class="chart-box"><h3>Стоимость портфеля, {escape(currency)}</h3>{market_chart}</article></div>
    </section>
    <section id="section-holdings" class="dashboard-section" data-dashboard-section="holdings" tabindex="-1" aria-labelledby="heading-holdings">
      <div class="section-head"><div><h2 id="heading-holdings">Позиции</h2><p>Количество, цена и стоимость на {escape(display_date(report_date))}</p></div><span class="count">{escape(_ru_count(len(sections["holdings"]["rows"]), "позиция", "позиции", "позиций"))}</span></div>
      <div class="table-scroll"><table><thead><tr><th>Инструмент</th><th>Портфель</th><th>Количество</th><th>Цена</th><th>Стоимость</th><th>Валюта</th></tr></thead><tbody>{holding_rows}</tbody></table></div>
    </section>
    <section id="section-currencies" class="dashboard-section" data-dashboard-section="currencies" tabindex="-1" aria-labelledby="heading-currencies">
      <div class="section-head"><div><h2 id="heading-currencies">Валюты</h2><p>Стоимость бумаг и остатки на счетах — отдельно в каждой исходной валюте</p></div></div>
      <div class="currency-grid">{currency_panels}{consolidated_markup}</div>
    </section>
    <section id="section-allocation" class="dashboard-section" data-dashboard-section="allocation" tabindex="-1" aria-labelledby="heading-allocation">
      <div class="section-head"><div><h2 id="heading-allocation">Структура активов</h2><p>Распределение портфеля по выбранному признаку</p></div></div>
      <div class="taxonomy-toolbar"><label class="control" for="taxonomy-select">Распределение<select id="taxonomy-select">{taxonomy_options}</select></label></div>{taxonomy_panels}
    </section>
    <section id="section-transactions" class="dashboard-section" data-dashboard-section="transactions" tabindex="-1" aria-labelledby="heading-transactions">
      <div class="section-head"><div><h2 id="heading-transactions">Операции</h2><p>Покупки, продажи, пополнения, доходы и списания</p></div><span id="transaction-count" class="count"></span></div>
      <div class="filters" role="search"><label class="control">Поиск<input type="search" data-transaction-filter="search" placeholder="Название операции"/></label><label class="control">Операция<select data-transaction-filter="type"><option value="">Все</option>{type_options}</select></label><label class="control">Валюта<select data-transaction-filter="currency"><option value="">Все</option>{currency_options}</select></label></div>
      <div class="table-scroll compact-table"><table><thead><tr><th>Дата</th><th>Операция</th><th>Сумма</th><th>Счёт</th></tr></thead><tbody>{transaction_rows}</tbody></table></div>
    </section>
    <section id="section-income" class="dashboard-section" data-dashboard-section="income" tabindex="-1" aria-labelledby="heading-income">
      <div class="section-head"><div><h2 id="heading-income">Доходы</h2><p>Полученные выплаты и проценты, начисленные к будущей выплате</p></div></div>{income_table}
    </section>
    <section id="section-fees_taxes" class="dashboard-section" data-dashboard-section="fees_taxes" tabindex="-1" aria-labelledby="heading-fees-taxes">
      <div class="section-head"><div><h2 id="heading-fees-taxes">Комиссии и налоги</h2><p>Все подтверждённые комиссии, налоги и прочие списания</p></div></div>{charges_table}
    </section>
    <section id="section-accounts" class="dashboard-section" data-dashboard-section="accounts" tabindex="-1" aria-labelledby="heading-accounts">
      <div class="section-head"><div><h2 id="heading-accounts">Денежные счета</h2><p>Остатки на основных счетах на дату отчёта</p></div></div>
      <div class="table-scroll accounts-table"><table><thead><tr><th>Счёт</th><th>Баланс</th><th>Валюта</th></tr></thead><tbody>{account_rows}</tbody></table></div>
    </section>
  </main>
  <script type="application/json" id="client-view-data">{client_views_embedded}</script>
  <script type="application/json" id="detailed-report-data">{embedded}</script>
  <script>{script}</script>
  <!-- Local sources: {escape(report_source.name)}, {escape(series_source.name)}, {escape(detailed_source.name)} -->
</body>
</html>
"""


def atomic_write(path: Path, content: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise DashboardError(
            f"cannot create dashboard output directory {path.parent}: {error}"
        ) from error

    descriptor = -1
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    except OSError as error:
        raise DashboardError(f"cannot write dashboard atomically to {path}: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate an autonomous dashboard.html from report.json and "
            "performance_series.csv."
        )
    )
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--series", required=True, type=Path)
    parser.add_argument(
        "--template",
        type=Path,
        help="optional legacy HTML source for an inline base64 logo",
    )
    parser.add_argument(
        "--detailed",
        type=Path,
        help="optional validated detailed report for the client dashboard",
    )
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def run(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        report = load_report(args.report)
        points = load_series(args.series)
        validate_series_against_report(points, report)
        logo = load_inline_logo(args.template) if args.template else DEFAULT_MARK
        if args.detailed:
            detailed = load_detailed_report(args.detailed)
            dashboard = render_detailed_dashboard(
                report,
                points,
                detailed,
                logo,
                args.report,
                args.series,
                args.detailed,
            )
        else:
            dashboard = render_dashboard(
                report, points, logo, args.report, args.series
            )
        atomic_write(args.output, dashboard)
    except DashboardError as error:
        print(f"dashboard generation failed: {error}", file=sys.stderr)
        return 2
    print(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
