#!/usr/bin/env python3
"""Official ECB EUR-based reference-rate acquisition and deterministic cache."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from renderer.currency_views import DatedFxProvider, FxRate


CACHE_SCHEMA_VERSION = "1.0"
ECB_PROVIDER = "European Central Bank"
ECB_SERIES_KEY = "D.USD.EUR.SP00.A"
ECB_SERIES_ENDPOINT = (
    "https://data-api.ecb.europa.eu/service/data/EXR/" + ECB_SERIES_KEY
)
ECB_SOURCE_LABEL = "European Central Bank reference rates"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
LATEST_PRIOR_LOOKBACK_DAYS = 10
ECB_SUPPORTED_QUOTES = frozenset(
    {
        "AUD", "BGN", "BRL", "CAD", "CHF", "CNY", "CZK", "DKK", "GBP",
        "HKD", "HUF", "IDR", "ILS", "INR", "ISK", "JPY", "KRW", "MXN",
        "MYR", "NOK", "NZD", "PHP", "PLN", "RON", "SEK", "SGD", "THB",
        "TRY", "USD", "ZAR",
    }
)
RATE_REASON_CODES = frozenset(
    {
        "FX_NETWORK_UNAVAILABLE",
        "FX_TLS_UNAVAILABLE",
        "FX_REDIRECT_REJECTED",
        "FX_RESPONSE_TOO_LARGE",
        "FX_CSV_INVALID",
        "FX_SERIES_MISMATCH",
        "FX_RATE_INVALID",
        "FX_RANGE_INCOMPLETE",
    }
)


class EcbRateError(ValueError):
    """Official FX input is unavailable, invalid, or outside the cached range."""

    def __init__(self, message: str, reason_code: str = "FX_DATA_UNAVAILABLE") -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class EcbObservation:
    day: date
    quote_per_eur: Decimal

    @property
    def usd_per_eur(self) -> Decimal:
        """Backward-compatible alias for the original USD-only cache contract."""
        return self.quote_per_eur


@dataclass(frozen=True)
class EcbRateLeg:
    quote_currency: str
    observation_date: date
    quote_per_eur: Decimal
    source_url: str
    retrieved_at: str
    payload_sha256: str
    observations_sha256: str


@dataclass(frozen=True)
class EcbRateResolution:
    source_currency: str
    target_currency: str
    requested_date: date
    observation_date: date
    rate: Decimal
    selection: str
    provider: str
    source_url: str
    retrieved_at: str
    payload_sha256: str
    observations_sha256: str
    source_observation_date: date | None = None
    target_observation_date: date | None = None
    legs: tuple[EcbRateLeg, ...] = ()

    def provenance(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "source_currency": self.source_currency,
            "target_currency": self.target_currency,
            "requested_date": self.requested_date.isoformat(),
            "observation_date": self.observation_date.isoformat(),
            "rate": decimal_text(self.rate),
            "selection": self.selection,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "payload_sha256": self.payload_sha256,
            "observations_sha256": self.observations_sha256,
            "source_observation_date": (
                self.source_observation_date.isoformat()
                if self.source_observation_date is not None
                else None
            ),
            "target_observation_date": (
                self.target_observation_date.isoformat()
                if self.target_observation_date is not None
                else None
            ),
            "legs": [
                {
                    "quote_currency": leg.quote_currency,
                    "observation_date": leg.observation_date.isoformat(),
                    "quote_per_eur": decimal_text(leg.quote_per_eur),
                    "source_url": leg.source_url,
                    "retrieved_at": leg.retrieved_at,
                    "payload_sha256": leg.payload_sha256,
                    "observations_sha256": leg.observations_sha256,
                }
                for leg in self.legs
            ],
        }


@dataclass(frozen=True)
class EcbRateCache:
    source_url: str
    requested_start_date: date
    requested_end_date: date
    retrieved_at: str
    payload_sha256: str
    observations_sha256: str
    quote_currency: str
    observations: tuple[EcbObservation, ...]

    def resolve(
        self, source_currency: str, target_currency: str, requested_date: date
    ) -> EcbRateResolution:
        source = source_currency.strip().upper()
        target = target_currency.strip().upper()
        quote = self.quote_currency
        if (source, target) not in {("EUR", quote), (quote, "EUR")}:
            raise EcbRateError(
                f"ECB cache supports EUR/{quote} conversion, not {source}/{target}"
            )
        eligible = [item for item in self.observations if item.day <= requested_date]
        if not eligible:
            raise EcbRateError(
                "official ECB data has no observation on or before the requested date"
            )
        observation = max(eligible, key=lambda item: item.day)
        if source == "EUR":
            rate = observation.quote_per_eur
        else:
            with localcontext() as context:
                context.prec = 28
                rate = Decimal(1) / observation.quote_per_eur
        leg = EcbRateLeg(
            quote,
            observation.day,
            observation.quote_per_eur,
            self.source_url,
            self.retrieved_at,
            self.payload_sha256,
            self.observations_sha256,
        )
        return EcbRateResolution(
            source,
            target,
            requested_date,
            observation.day,
            rate,
            "EXACT" if observation.day == requested_date else "PRIOR",
            ECB_PROVIDER,
            self.source_url,
            self.retrieved_at,
            self.payload_sha256,
            self.observations_sha256,
            observation.day if source == quote else None,
            observation.day if target == quote else None,
            (leg,),
        )

    def to_dated_provider(self) -> DatedFxProvider:
        return DatedFxProvider(
            FxRate(
                "EUR",
                self.quote_currency,
                item.day,
                item.quote_per_eur,
                ECB_SOURCE_LABEL,
            )
            for item in self.observations
        )


@dataclass(frozen=True)
class EcbRateBook:
    caches: Mapping[str, EcbRateCache]
    cache_paths: Mapping[str, Path]

    def __post_init__(self) -> None:
        normalized = {code.upper(): cache for code, cache in self.caches.items()}
        if not normalized or any(
            code != cache.quote_currency for code, cache in normalized.items()
        ):
            raise EcbRateError("ECB rate book currency inventory is invalid")
        normalized_paths = {
            code.upper(): Path(path) for code, path in self.cache_paths.items()
        }
        if set(normalized_paths) != set(normalized):
            raise EcbRateError("ECB rate book path inventory is invalid")
        object.__setattr__(self, "caches", dict(sorted(normalized.items())))
        object.__setattr__(
            self,
            "cache_paths",
            dict(sorted(normalized_paths.items())),
        )

    @property
    def observations_sha256(self) -> str:
        if set(self.caches) == {"USD"}:
            return self.caches["USD"].observations_sha256
        return _combined_digest(
            (code, cache.observations_sha256)
            for code, cache in self.caches.items()
        )

    @property
    def java_snapshot_path(self) -> Path:
        if set(self.cache_paths) == {"USD"}:
            return self.cache_paths["USD"]
        parents = {path.parent.resolve() for path in self.cache_paths.values()}
        if len(parents) != 1:
            raise EcbRateError("ECB rate-book files must share one directory")
        return next(iter(parents))

    def _leg(self, currency: str, requested_date: date) -> EcbRateLeg:
        try:
            cache = self.caches[currency]
        except KeyError as error:
            raise unsupported_currency_error(currency) from error
        resolved = cache.resolve("EUR", currency, requested_date)
        return resolved.legs[0]

    def resolve(
        self,
        source_currency: str,
        target_currency: str,
        requested_date: date,
    ) -> EcbRateResolution:
        source = source_currency.strip().upper()
        target = target_currency.strip().upper()
        if source == target:
            raise EcbRateError("ECB conversion is not required for identical currencies")
        source_leg = None if source == "EUR" else self._leg(source, requested_date)
        target_leg = None if target == "EUR" else self._leg(target, requested_date)
        legs = tuple(leg for leg in (source_leg, target_leg) if leg is not None)
        with localcontext() as context:
            context.prec = 28
            source_per_eur = (
                source_leg.quote_per_eur if source_leg is not None else Decimal(1)
            )
            target_per_eur = (
                target_leg.quote_per_eur if target_leg is not None else Decimal(1)
            )
            rate = target_per_eur / source_per_eur
        dates = tuple(leg.observation_date for leg in legs)
        observation_date = max(dates)
        selection = "EXACT" if all(day == requested_date for day in dates) else "PRIOR"
        if len(legs) == 1:
            payload_sha256 = legs[0].payload_sha256
            observations_sha256 = legs[0].observations_sha256
            source_url = legs[0].source_url
            retrieved_at = legs[0].retrieved_at
        else:
            payload_sha256 = _combined_digest(
                (leg.quote_currency, leg.payload_sha256) for leg in legs
            )
            observations_sha256 = _combined_digest(
                (leg.quote_currency, leg.observations_sha256) for leg in legs
            )
            source_url = ";".join(leg.source_url for leg in legs)
            retrieved_at = max(leg.retrieved_at for leg in legs)
        return EcbRateResolution(
            source,
            target,
            requested_date,
            observation_date,
            rate,
            selection,
            ECB_PROVIDER,
            source_url,
            retrieved_at,
            payload_sha256,
            observations_sha256,
            source_leg.observation_date if source_leg is not None else None,
            target_leg.observation_date if target_leg is not None else None,
            legs,
        )

    def to_dated_provider(self) -> DatedFxProvider:
        return DatedFxProvider(
            rate
            for cache in self.caches.values()
            for rate in (
                FxRate(
                    "EUR",
                    cache.quote_currency,
                    item.day,
                    item.quote_per_eur,
                    ECB_SOURCE_LABEL,
                )
                for item in cache.observations
            )
        )


def _combined_digest(items: Iterable[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for code, value in sorted(items):
        digest.update(code.encode("ascii"))
        digest.update(b":")
        digest.update(value.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def unsupported_currency_error(currency: str) -> EcbRateError:
    code = currency.strip().upper()
    safe = code if len(code) == 3 and code.isalpha() else "???"
    return EcbRateError(
        f"Валюта {safe} отсутствует в официальном наборе ЕЦБ. "
        "Проверьте код валюты или запросите подключение другого официального источника.",
        "FX_CURRENCY_UNSUPPORTED",
    )


def decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise EcbRateError("ECB rate must be finite")
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result


def _observations_payload(observations: Sequence[EcbObservation]) -> bytes:
    normalized = [
        {"date": item.day.isoformat(), "rate": decimal_text(item.quote_per_eur)}
        for item in observations
    ]
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _checksum(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parse_iso_date(value: object, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise EcbRateError(f"ECB cache has invalid {label}") from error


def _validate_digest(value: object, label: str) -> str:
    text = str(value)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise EcbRateError(f"ECB cache has invalid {label}")
    return text


def _series_key(quote_currency: str) -> str:
    quote = quote_currency.strip().upper()
    if quote not in ECB_SUPPORTED_QUOTES:
        raise unsupported_currency_error(quote)
    return f"D.{quote}.EUR.SP00.A"


def _series_endpoint(quote_currency: str) -> str:
    return "https://data-api.ecb.europa.eu/service/data/EXR/" + _series_key(
        quote_currency
    )


def _validate_source_url(value: object, quote_currency: str = "USD") -> str:
    source_url = str(value)
    parsed = urlparse(source_url)
    series_key = _series_key(quote_currency)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "data-api.ecb.europa.eu"
        or not parsed.path.endswith("/EXR/" + series_key)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise EcbRateError(
            "ECB cache source URL is not the official ECB series endpoint",
            "FX_REDIRECT_REJECTED",
        )
    return source_url


def _validate_retrieved_at(value: object) -> str:
    text = str(value)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise EcbRateError("ECB cache has invalid retrieval timestamp") from error
    return text


def load_cache(path: Path) -> EcbRateCache:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except FileNotFoundError as error:
        raise EcbRateError("official ECB FX cache is missing") from error
    except (OSError, json.JSONDecodeError, InvalidOperation) as error:
        raise EcbRateError("official ECB FX cache cannot be read as valid JSON") from error
    if not isinstance(raw, Mapping):
        raise EcbRateError("ECB cache root must be an object")
    expected = {
        "schema_version",
        "provider",
        "dataset",
        "series_key",
        "source_url",
        "requested_start_date",
        "requested_end_date",
        "retrieved_at",
        "payload_sha256",
        "observations_sha256",
        "base_currency",
        "quote_currency",
        "observations",
    }
    if set(raw) != expected:
        raise EcbRateError("ECB cache keys differ from the normalized contract")
    if raw["schema_version"] != CACHE_SCHEMA_VERSION:
        raise EcbRateError("ECB cache schema version is unsupported")
    if raw["provider"] != ECB_PROVIDER or raw["dataset"] != "EXR":
        raise EcbRateError("ECB cache provider metadata is invalid")
    quote_currency = str(raw["quote_currency"]).strip().upper()
    if quote_currency not in ECB_SUPPORTED_QUOTES:
        raise unsupported_currency_error(quote_currency)
    if raw["series_key"] != _series_key(quote_currency):
        raise EcbRateError("ECB cache series key is invalid")
    if raw["base_currency"] != "EUR":
        raise EcbRateError("ECB cache currency pair is invalid")
    source_url = _validate_source_url(raw["source_url"], quote_currency)
    start = _parse_iso_date(raw["requested_start_date"], "requested start date")
    end = _parse_iso_date(raw["requested_end_date"], "requested end date")
    if start > end:
        raise EcbRateError("ECB cache requested date range is reversed")
    retrieved_at = _validate_retrieved_at(raw["retrieved_at"])
    payload_sha256 = _validate_digest(raw["payload_sha256"], "payload checksum")
    expected_observations_sha256 = _validate_digest(
        raw["observations_sha256"], "observations checksum"
    )
    if not isinstance(raw["observations"], list) or not raw["observations"]:
        raise EcbRateError("ECB cache contains no observations")
    observations: list[EcbObservation] = []
    seen_dates: set[date] = set()
    for row in raw["observations"]:
        if not isinstance(row, Mapping) or set(row) != {"date", "rate"}:
            raise EcbRateError("ECB cache observation has invalid fields")
        day = _parse_iso_date(row["date"], "observation date")
        try:
            rate = Decimal(str(row["rate"]))
        except InvalidOperation as error:
            raise EcbRateError("ECB cache observation has invalid rate") from error
        if not rate.is_finite() or rate <= 0:
            raise EcbRateError("ECB cache observation rate must be positive and finite")
        if day in seen_dates:
            raise EcbRateError("ECB cache contains duplicate observation dates")
        if day < start or day > end:
            raise EcbRateError("ECB cache observation falls outside the requested range")
        seen_dates.add(day)
        observations.append(EcbObservation(day, rate))
    if observations != sorted(observations, key=lambda item: item.day):
        raise EcbRateError("ECB cache observations are not date-sorted")
    actual_observations_sha256 = _checksum(_observations_payload(observations))
    if actual_observations_sha256 != expected_observations_sha256:
        raise EcbRateError("ECB cache observations checksum mismatch")
    return EcbRateCache(
        source_url,
        start,
        end,
        retrieved_at,
        payload_sha256,
        expected_observations_sha256,
        quote_currency,
        tuple(observations),
    )


def _source_url(start: date, end: date, quote_currency: str = "USD") -> str:
    return _series_endpoint(quote_currency) + "?" + urlencode(
        (
            ("startPeriod", start.isoformat()),
            ("endPeriod", end.isoformat()),
            ("format", "csvdata"),
        )
    )


def _verified_tls_context() -> ssl.SSLContext:
    try:
        context = ssl.create_default_context()
    except (OSError, ssl.SSLError) as error:
        raise EcbRateError(
            "verified HTTPS context is unavailable", "FX_TLS_UNAVAILABLE"
        ) from error
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
        raise EcbRateError(
            "verified HTTPS context is unavailable", "FX_TLS_UNAVAILABLE"
        )
    return context


def _curl_command(source_url: str, *, windows: bool | None = None) -> list[str]:
    use_windows = os.name == "nt" if windows is None else windows
    return [
        "curl.exe" if use_windows else "curl",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--tlsv1.2",
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        "20",
        "--connect-timeout",
        "10",
        "--max-filesize",
        str(MAX_RESPONSE_BYTES),
        source_url,
    ]


def _public_trace(event: str, **fields: object) -> None:
    if os.environ.get("PP_FX_PUBLIC_TRACE") != "1":
        return
    allowed = {"HOST", "QUOTE", "START", "END", "BYTES", "PAYLOAD_SHA256"}
    records = [f"{key}={fields[key]}" for key in sorted(fields) if key in allowed]
    sys.stderr.write("PP_FX_PUBLIC|" + event + "|" + "|".join(records) + "\n")


def _parse_ecb_csv(
    payload: bytes,
    start: date,
    end: date,
    quote_currency: str = "USD",
) -> tuple[EcbObservation, ...]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise EcbRateError(
            "official ECB response is not UTF-8 CSV", "FX_CSV_INVALID"
        ) from error
    reader = csv.DictReader(io.StringIO(text))
    required = {
        "KEY",
        "FREQ",
        "CURRENCY",
        "CURRENCY_DENOM",
        "TIME_PERIOD",
        "OBS_VALUE",
    }
    if reader.fieldnames is None or not required.issubset(reader.fieldnames):
        raise EcbRateError(
            "official ECB response has an unexpected CSV header", "FX_CSV_INVALID"
        )
    observations: list[EcbObservation] = []
    seen_dates: set[date] = set()
    quote = quote_currency.strip().upper()
    series_key = _series_key(quote)
    for row in reader:
        if (
            row.get("KEY") != "EXR." + series_key
            or row.get("FREQ") != "D"
            or row.get("CURRENCY") != quote
            or row.get("CURRENCY_DENOM") != "EUR"
        ):
            raise EcbRateError(
                "official ECB response contains an unexpected series",
                "FX_SERIES_MISMATCH",
            )
        day = _parse_iso_date(row.get("TIME_PERIOD"), "response observation date")
        try:
            rate = Decimal(str(row.get("OBS_VALUE")))
        except InvalidOperation as error:
            raise EcbRateError(
                "official ECB response contains an invalid rate", "FX_RATE_INVALID"
            ) from error
        if day < start or day > end:
            raise EcbRateError(
                "official ECB response contains an out-of-range observation",
                "FX_CSV_INVALID",
            )
        if day in seen_dates:
            raise EcbRateError(
                "official ECB response contains duplicate observation dates",
                "FX_CSV_INVALID",
            )
        if not rate.is_finite() or rate <= 0:
            raise EcbRateError(
                "official ECB response rate must be positive and finite",
                "FX_RATE_INVALID",
            )
        seen_dates.add(day)
        observations.append(EcbObservation(day, rate))
    if not observations:
        raise EcbRateError(
            "official ECB response contains no observations", "FX_RANGE_INCOMPLETE"
        )
    return tuple(sorted(observations, key=lambda item: item.day))


def _normalized_cache_json(
    observations: Sequence[EcbObservation],
    *,
    source_url: str,
    start: date,
    end: date,
    retrieved_at: str,
    payload_sha256: str,
    quote_currency: str = "USD",
) -> str:
    observations_sha256 = _checksum(_observations_payload(observations))
    model = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "provider": ECB_PROVIDER,
        "dataset": "EXR",
        "series_key": _series_key(quote_currency),
        "source_url": source_url,
        "requested_start_date": start.isoformat(),
        "requested_end_date": end.isoformat(),
        "retrieved_at": retrieved_at,
        "payload_sha256": payload_sha256,
        "observations_sha256": observations_sha256,
        "base_currency": "EUR",
        "quote_currency": quote_currency,
        "observations": [
            {"date": item.day.isoformat(), "rate": decimal_text(item.quote_per_eur)}
            for item in observations
        ],
    }
    return json.dumps(model, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def fetch_cache(
    path: Path,
    start: date,
    end: date,
    *,
    opener: Callable[..., object] = urlopen,
    retrieved_at: str | None = None,
    quote_currency: str = "USD",
) -> EcbRateCache:
    if start > end:
        raise EcbRateError("ECB request date range is reversed")
    quote = quote_currency.strip().upper()
    source_url = _source_url(start, end, quote)
    request = Request(
        source_url,
        headers={
            "Accept": "text/csv",
            "User-Agent": "Portfolio-Dashboard/1.0",
        },
        method="GET",
    )
    _public_trace(
        "REQUEST",
        HOST="data-api.ecb.europa.eu",
        QUOTE=quote,
        START=start.isoformat(),
        END=end.isoformat(),
    )
    try:
        if opener is urlopen:
            response = opener(request, timeout=20, context=_verified_tls_context())
        else:
            response = opener(request, timeout=20)
        with response:
            final_url = _validate_source_url(response.geturl(), quote)
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except EcbRateError:
        raise
    except Exception as error:
        failure_reason = (
            "FX_TLS_UNAVAILABLE"
            if isinstance(error, ssl.SSLError)
            else "FX_NETWORK_UNAVAILABLE"
        )
        if opener is not urlopen:
            raise EcbRateError(
                "official ECB FX request failed", failure_reason
            ) from error
        try:
            result = subprocess.run(
                _curl_command(source_url),
                check=False,
                capture_output=True,
            )
        except OSError as curl_error:
            raise EcbRateError(
                "official ECB FX request failed", failure_reason
            ) from curl_error
        if result.returncode != 0:
            curl_reason = (
                "FX_TLS_UNAVAILABLE"
                if result.returncode in {35, 51, 53, 58, 59, 60, 64, 66, 77, 80, 82, 83, 90, 91}
                else failure_reason
            )
            raise EcbRateError(
                "official ECB FX request failed", curl_reason
            ) from error
        final_url = source_url
        payload = result.stdout
    if len(payload) > MAX_RESPONSE_BYTES:
        raise EcbRateError(
            "official ECB response exceeds the safety limit", "FX_RESPONSE_TOO_LARGE"
        )
    _public_trace(
        "RESPONSE",
        QUOTE=quote,
        BYTES=len(payload),
        PAYLOAD_SHA256=_checksum(payload),
    )
    observations = _parse_ecb_csv(payload, start, end, quote)
    retrieved = retrieved_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    content = _normalized_cache_json(
        observations,
        source_url=final_url,
        start=start,
        end=end,
        retrieved_at=retrieved,
        payload_sha256=_checksum(payload),
        quote_currency=quote,
    )
    _atomic_write(path, content)
    return load_cache(path)


def ensure_cache(
    path: Path,
    start: date,
    end: date,
    *,
    offline: bool = False,
    force_refresh: bool = False,
    opener: Callable[..., object] = urlopen,
    quote_currency: str | None = None,
) -> EcbRateCache:
    if path.exists() and not force_refresh:
        cache = load_cache(path)
        if quote_currency is not None and cache.quote_currency != quote_currency.upper():
            raise EcbRateError("ECB cache quote currency differs from the requested series")
        if cache.requested_start_date <= start and cache.requested_end_date >= end:
            try:
                cache.resolve("EUR", cache.quote_currency, start)
                cache.resolve("EUR", cache.quote_currency, end)
            except EcbRateError:
                if offline:
                    raise
            else:
                return cache
    if offline:
        raise EcbRateError(
            "official ECB FX cache is missing or does not cover the report range; "
            "refresh it while online",
            "FX_RANGE_INCOMPLETE",
        )
    fetch_start = start - timedelta(
        days=min(LATEST_PRIOR_LOOKBACK_DAYS, start.toordinal() - 1)
    )
    cache = fetch_cache(
        path,
        fetch_start,
        end,
        opener=opener,
        quote_currency=quote_currency or "USD",
    )
    cache.resolve("EUR", cache.quote_currency, start)
    cache.resolve("EUR", cache.quote_currency, end)
    return cache


def cache_path_for(location: Path, quote_currency: str) -> Path:
    quote = quote_currency.strip().upper()
    if quote not in ECB_SUPPORTED_QUOTES:
        raise unsupported_currency_error(quote)
    expanded = location.expanduser()
    if quote == "USD" and expanded.suffix.casefold() == ".json":
        return expanded
    directory = expanded.parent if expanded.suffix.casefold() == ".json" else expanded
    return directory / f"ecb-eur-{quote.casefold()}.json"


def ensure_rate_book(
    location: Path,
    source_currencies: Sequence[str],
    start: date,
    end: date,
    *,
    offline: bool = False,
    force_refresh: bool = False,
    opener: Callable[..., object] = urlopen,
) -> EcbRateBook:
    normalized = {str(code).strip().upper() for code in source_currencies}
    invalid = sorted(
        code
        for code in normalized
        if code != "EUR" and code not in ECB_SUPPORTED_QUOTES
    )
    if invalid:
        raise unsupported_currency_error(invalid[0])
    required_quotes = sorted({"USD"} | (normalized - {"EUR"}))
    if force_refresh and not offline:
        return _refresh_rate_book_atomically(
            location,
            required_quotes,
            start,
            end,
            opener=opener,
        )
    caches: dict[str, EcbRateCache] = {}
    paths: dict[str, Path] = {}
    for quote in required_quotes:
        path = cache_path_for(location, quote)
        caches[quote] = ensure_cache(
            path,
            start,
            end,
            offline=offline,
            force_refresh=force_refresh,
            opener=opener,
            quote_currency=quote,
        )
        paths[quote] = path.resolve()
    return EcbRateBook(caches, paths)


def _refresh_rate_book_atomically(
    location: Path,
    required_quotes: Sequence[str],
    start: date,
    end: date,
    *,
    opener: Callable[..., object],
) -> EcbRateBook:
    """Fetch every required quote before publishing any refreshed cache file."""
    final_paths = {
        quote: cache_path_for(location, quote) for quote in required_quotes
    }
    parents = {path.parent.resolve(strict=False) for path in final_paths.values()}
    if len(parents) != 1:
        raise EcbRateError("ECB rate-book files must share one directory")
    cache_root = next(iter(parents))
    cache_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".ecb-rate-book-", dir=cache_root))
    previous = {
        quote: path.read_bytes() if path.exists() else None
        for quote, path in final_paths.items()
    }
    try:
        staged_paths: dict[str, Path] = {}
        for quote in required_quotes:
            staged = staging / final_paths[quote].name
            fetch_cache(
                staged,
                start - timedelta(
                    days=min(LATEST_PRIOR_LOOKBACK_DAYS, start.toordinal() - 1)
                ),
                end,
                opener=opener,
                quote_currency=quote,
            )
            cache = load_cache(staged)
            cache.resolve("EUR", quote, start)
            cache.resolve("EUR", quote, end)
            staged_paths[quote] = staged
        published: list[str] = []
        try:
            for quote in required_quotes:
                os.replace(staged_paths[quote], final_paths[quote])
                published.append(quote)
        except OSError:
            for quote in published:
                original = previous[quote]
                if original is None:
                    final_paths[quote].unlink(missing_ok=True)
                else:
                    _atomic_write(
                        final_paths[quote], original.decode("utf-8")
                    )
            raise
        caches = {quote: load_cache(path) for quote, path in final_paths.items()}
        return EcbRateBook(caches, {quote: path.resolve() for quote, path in final_paths.items()})
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh an official ECB EUR-based FX cache")
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--currency", default="USD")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        quote = args.currency.strip().upper()
        cache = fetch_cache(
            args.cache,
            args.start,
            args.end,
            quote_currency=quote,
        )
        resolution = cache.resolve("EUR", quote, args.end)
    except (OSError, EcbRateError) as error:
        sys.stderr.write(f"ECB FX cache refresh failed: {error}\n")
        return 2
    sys.stdout.write(json.dumps(resolution.provenance(), ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
