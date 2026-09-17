#!/usr/bin/env python3
"""Create and incrementally update one managed Portfolio Performance XML."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable
import unicodedata
import uuid
import xml.etree.ElementTree as ElementTree

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None  # type: ignore[assignment]


CSV_HEADERS = (
    "Date",
    "Type",
    "Transaction Currency",
    "ISIN",
    "Security Name",
    "Shares",
    "Fees",
    "Taxes",
    "Value",
    "Cash Account",
    "Securities Account",
    "Note",
)
CSV_V2_HEADERS = (
    "SchemaVersion",
    "PortfolioKey",
    "SourceSystem",
    "SourceDocumentId",
    "ExternalTransactionId",
    "Date",
    "Type",
    "Transaction Currency",
    "SecurityKey",
    "ISIN",
    "Security Name",
    "Shares",
    "Fees",
    "Taxes",
    "Value",
    "CashAccountKey",
    "Cash Account",
    "SecuritiesAccountKey",
    "Securities Account",
    "Note",
)
ENGINE_CSV_HEADERS = CSV_HEADERS + (
    "_IdentityKey",
    "_SourceDocumentId",
    "_CashAccountUUID",
    "_SecuritiesAccountUUID",
    "_SecurityUUID",
)
SUPPORTED_TYPES = (
    "Deposit",
    "Removal",
    "Buy",
    "Sell",
    "Dividend",
    "Taxes",
    "Interest",
    "Interest Charge",
)
SECURITY_TYPES = ("Buy", "Sell", "Dividend")
STATE_VERSION = 4


class ConfigurationError(ValueError):
    pass


class CSVValidationError(ValueError):
    pass


class ImportWorkflowError(RuntimeError):
    pass


class ImportDeferred(ImportWorkflowError):
    pass


class FinalStateCommitDeferred(ImportDeferred):
    """Publication is durable, but the final ledger commit needs restart recovery."""


class ImportNeedsReview(ImportWorkflowError):
    pass


class RollbackFailed(ImportWorkflowError):
    pass


class CsvMoveUncertain(ImportWorkflowError):
    def __init__(self, message: str, *, source: Path, destination: Path):
        super().__init__(message)
        self.source = source
        self.destination = destination


class WatcherAlreadyRunningError(ImportWorkflowError):
    pass


@dataclass(frozen=True)
class RuntimeConfig:
    config_file: Path
    project_root: Path
    mutable_root: Path
    dry_run: bool
    xml_file: Path
    incoming_dir: Path
    processing_dir: Path
    engine_temp_dir: Path
    processed_dir: Path
    needs_review_dir: Path
    rejected_dir: Path
    backup_dir: Path
    reports_dir: Path
    log_file: Path
    state_file: Path
    lock_file: Path
    account_mapping_file: Path
    engine_launcher: Path
    base_currency: str
    managed_portfolio_key: str
    require_existing_xml: bool
    csv_encoding: str
    csv_headers: tuple[str, ...]
    supported_types: tuple[str, ...]
    date_formats: tuple[str, ...]
    decimal_profile: str
    csv_max_file_bytes: int
    csv_max_rows: int
    poll_interval_seconds: float
    stability_check_interval_seconds: float
    stable_observations: int


@dataclass(frozen=True)
class PublishResult:
    xml_path: Path
    xml_sha256: str
    backup_path: Path | None
    engine_summary: str
    added_transactions: int
    skipped_transactions: int
    logical_transactions: int


@dataclass(frozen=True)
class ProcessResult:
    outcome: str
    csv_sha256: str
    destination: Path | None
    backup_path: Path | None
    xml_path: Path | None
    error: str = ""


@dataclass(frozen=True)
class MappingTarget:
    key: str
    uuid: str
    name: str
    currency: str
    isin: str = ""
    reference_cash_account_key: str = ""


@dataclass(frozen=True)
class AccountMapping:
    cash_accounts: tuple[MappingTarget, ...] = ()
    securities_accounts: tuple[MappingTarget, ...] = ()
    securities: tuple[MappingTarget, ...] = ()


@dataclass(frozen=True)
class CanonicalRecord:
    schema_version: str
    identity_provenance: str
    portfolio_key: str
    source_system: str
    source_document_id: str
    external_transaction_id: str
    occurrence: int
    line_number: int
    date: str
    transaction_type: str
    currency: str
    security_key: str
    security_uuid: str
    isin: str
    security_name: str
    shares: str
    fees: str
    taxes: str
    value: str
    cash_account_key: str
    cash_account_uuid: str
    cash_account_name: str
    securities_account_key: str
    securities_account_uuid: str
    securities_account_name: str
    note: str

    def business_payload(self) -> dict[str, str]:
        return {
            "date": self.date,
            "type": self.transaction_type,
            "currency": self.currency,
            "security_target": (
                self.security_uuid
                or self.isin
                or f"name:{self.security_name}"
            ),
            "shares": self.shares,
            "fees": self.fees,
            "taxes": self.taxes,
            "value": self.value,
            "cash_account_target": (
                self.cash_account_uuid or f"name:{self.cash_account_name}"
            ),
            "securities_account_target": (
                self.securities_account_uuid
                or (
                    f"name:{self.securities_account_name}"
                    if self.securities_account_name
                    else ""
                )
            ),
        }

    def payload_sha256(self) -> str:
        payload = json.dumps(
            self.business_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def identity_key(self, file_sha256: str) -> str:
        if self.schema_version == "2":
            parts = (
                self.portfolio_key,
                self.source_system,
                self.external_transaction_id,
            )
            encoded = json.dumps(
                parts,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            return "v2:" + hashlib.sha256(encoded).hexdigest()
        return f"v1:{file_sha256}:{self.occurrence}"

    def engine_row(self, file_sha256: str) -> list[str]:
        engine_shares = "" if self.shares == "0" else self.shares
        return [
            self.date,
            self.transaction_type,
            self.currency,
            self.isin,
            self.security_name,
            engine_shares,
            self.fees,
            self.taxes,
            self.value,
            self.cash_account_name,
            self.securities_account_name,
            self.note,
            self.identity_key(file_sha256),
            self.source_document_id,
            self.cash_account_uuid,
            self.securities_account_uuid,
            self.security_uuid,
        ]


@dataclass(frozen=True)
class IdentityDecision:
    outcome: str
    new_records: tuple[CanonicalRecord, ...]
    skipped_records: tuple[CanonicalRecord, ...]
    reason: str = ""


EngineRunner = Callable[..., subprocess.CompletedProcess[str]]


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    answer: dict[str, Any] = {}
    for key, value in pairs:
        if key in answer:
            raise ValueError(f"duplicate JSON key: {key}")
        answer[key] = value
    return answer


def _load_json_text(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_unique_json_object)


def _required(mapping: dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise ConfigurationError(f"Missing config key: {context}.{key}")
    return mapping[key]


def _resolve(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve(strict=False)


def _mutable_root_for(config_file: Path) -> Path:
    automation_dir = config_file.parent
    if automation_dir.name == "automation" and automation_dir.parent.name == "_app":
        return automation_dir.parent.parent.resolve(strict=False)
    return automation_dir.parent.resolve(strict=False)


def _resolve_mutable(root: Path, value: str, mutable_root: Path, label: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    lexical = Path(os.path.abspath(candidate))
    allowed = Path(os.path.abspath(mutable_root))
    try:
        relative = lexical.relative_to(allowed)
    except ValueError as error:
        raise ConfigurationError(
            f"Mutable path {label} escapes the managed root: {lexical}"
        ) from error
    current = allowed
    if current.is_symlink():
        raise ConfigurationError(f"Managed root cannot be a symlink: {current}")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ConfigurationError(
                f"Mutable path {label} contains a symlink component: {current}"
            )
    resolved = lexical.resolve(strict=False)
    try:
        resolved.relative_to(allowed.resolve(strict=False))
    except ValueError as error:
        raise ConfigurationError(
            f"Mutable path {label} resolves outside the managed root: {resolved}"
        ) from error
    return resolved


def _assert_mutable_path(path: Path, mutable_root: Path, label: str) -> None:
    _resolve_mutable(mutable_root, str(path), mutable_root, label)


def load_config(config_file: Path) -> RuntimeConfig:
    config_file = config_file.expanduser().resolve()
    try:
        raw = _load_json_text(config_file.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigurationError(f"Config file not found: {config_file}") from error
    except (json.JSONDecodeError, ValueError) as error:
        raise ConfigurationError(f"Invalid JSON in {config_file}: {error}") from error

    paths = _required(raw, "paths", "root")
    csv_config = _required(raw, "csv", "root")
    watcher = _required(raw, "watcher", "root")
    portfolio = _required(raw, "portfolio", "root")
    engine = _required(raw, "engine", "root")
    if not all(
        isinstance(item, dict)
        for item in (paths, csv_config, watcher, portfolio, engine)
    ):
        raise ConfigurationError(
            "paths, csv, watcher, portfolio, and engine must be objects"
        )

    headers = tuple(_required(csv_config, "headers", "csv"))
    if headers != CSV_HEADERS:
        raise ConfigurationError("csv.headers must match the legacy V1 12-column contract")
    supported_types = tuple(_required(csv_config, "supported_types", "csv"))
    if supported_types != SUPPORTED_TYPES:
        raise ConfigurationError(
            "csv.supported_types must list the canonical eight transaction types"
        )
    raw_date_formats = csv_config.get(
        "date_formats",
        [csv_config.get("date_format", "%Y-%m-%d")],
    )
    if (
        not isinstance(raw_date_formats, list)
        or not raw_date_formats
        or not all(item in {"%Y-%m-%d", "%d.%m.%Y"} for item in raw_date_formats)
    ):
        raise ConfigurationError(
            "csv.date_formats must contain ISO %Y-%m-%d and/or %d.%m.%Y"
        )
    decimal_profile = str(csv_config.get("decimal_profile", "plain"))
    if decimal_profile not in {"plain", "en_US", "de_DE"}:
        raise ConfigurationError(
            "csv.decimal_profile must be plain, en_US, or de_DE"
        )
    csv_max_file_bytes = int(csv_config.get("max_file_bytes", 25 * 1024 * 1024))
    csv_max_rows = int(csv_config.get("max_rows", 100_000))
    if csv_max_file_bytes < 1024:
        raise ConfigurationError("csv.max_file_bytes must be at least 1024")
    if csv_max_rows < 1:
        raise ConfigurationError("csv.max_rows must be positive")
    stable_observations = int(
        _required(watcher, "stable_observations", "watcher")
    )
    if stable_observations < 2:
        raise ConfigurationError("watcher.stable_observations must be at least 2")
    base_currency = str(_required(portfolio, "base_currency", "portfolio")).strip().upper()
    if base_currency and not re.fullmatch(r"[A-Z]{3}", base_currency):
        raise ConfigurationError("portfolio.base_currency must be empty or three uppercase letters")
    managed_portfolio_key = unicodedata.normalize(
        "NFC",
        str(portfolio.get("portfolio_key", "managed-portfolio")),
    ).strip()
    if not managed_portfolio_key or _is_placeholder_account(managed_portfolio_key):
        raise ConfigurationError("portfolio.portfolio_key must be a stable non-placeholder value")

    project_root = config_file.parent
    mutable_root = _mutable_root_for(config_file)
    mutable = lambda key, default=None: _resolve_mutable(
        project_root,
        str(paths.get(key, default) if default is not None else _required(paths, key, "paths")),
        mutable_root,
        f"paths.{key}",
    )
    return RuntimeConfig(
        config_file=config_file,
        project_root=project_root,
        mutable_root=mutable_root,
        dry_run=bool(_required(raw, "dry_run", "root")),
        xml_file=mutable("xml_file"),
        incoming_dir=mutable("incoming_dir"),
        processing_dir=mutable("processing_dir", "../input/processing"),
        engine_temp_dir=mutable("engine_temp_dir", "../output/runtime/engine_temp"),
        processed_dir=mutable("processed_dir"),
        needs_review_dir=mutable("needs_review_dir", "../input/needs_review"),
        rejected_dir=mutable("rejected_dir"),
        backup_dir=mutable("backup_dir"),
        reports_dir=mutable("reports_dir", "../output/reports"),
        log_file=mutable("log_file"),
        state_file=mutable("state_file"),
        lock_file=mutable("lock_file"),
        account_mapping_file=mutable("account_mapping_file", "account-mapping.json"),
        engine_launcher=_resolve(
            project_root, _required(engine, "launcher", "engine")
        ),
        base_currency=base_currency,
        managed_portfolio_key=managed_portfolio_key,
        require_existing_xml=bool(portfolio.get("require_existing_xml", False)),
        csv_encoding=str(_required(csv_config, "encoding", "csv")),
        csv_headers=headers,
        supported_types=supported_types,
        date_formats=tuple(raw_date_formats),
        decimal_profile=decimal_profile,
        csv_max_file_bytes=csv_max_file_bytes,
        csv_max_rows=csv_max_rows,
        poll_interval_seconds=float(
            _required(watcher, "poll_interval_seconds", "watcher")
        ),
        stability_check_interval_seconds=float(
            _required(watcher, "stability_check_interval_seconds", "watcher")
        ),
        stable_observations=stable_observations,
    )


def ensure_directories(config: RuntimeConfig) -> None:
    if config.require_existing_xml and not config.xml_file.is_file():
        raise ConfigurationError(
            "Existing-master mode requires the managed Portfolio XML; "
            "restore the copied XML before starting the watcher"
        )
    for directory in (
        config.incoming_dir,
        config.processing_dir,
        config.engine_temp_dir,
        config.processed_dir,
        config.needs_review_dir,
        config.rejected_dir,
        config.backup_dir,
        config.reports_dir,
        config.xml_file.parent,
        config.log_file.parent,
        config.state_file.parent,
        config.lock_file.parent,
    ):
        _assert_mutable_path(directory, config.mutable_root, str(directory))
        directory.mkdir(parents=True, exist_ok=True)
        if directory == config.engine_temp_dir and os.name != "nt":
            directory.chmod(0o700)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_file_bounded(path: Path, max_bytes: int) -> str:
    """Hash at most the configured input bound, failing before unbounded reads."""
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while True:
            block = stream.read(min(1024 * 1024, max_bytes - total + 1))
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                raise CSVValidationError(
                    f"CSV exceeds the configured byte limit ({max_bytes})"
                )
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


class SingleInstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._stream: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - supported runtimes provide one module
                raise OSError("No supported file-locking API is available")
        except (BlockingIOError, OSError) as error:
            stream.close()
            raise WatcherAlreadyRunningError(
                f"Another watcher owns the lock: {self.path}"
            ) from error
        stream.seek(0)
        stream.truncate()
        stream.write(f"{os.getpid()}\n".encode("ascii"))
        stream.flush()
        os.fsync(stream.fileno())
        self._stream = stream

    def release(self) -> None:
        if self._stream is None:
            return
        if fcntl is not None:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            self._stream.seek(0)
            msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
        self._stream.close()
        self._stream = None

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()


def _audit_error_summary(error: str) -> str:
    if not error:
        return ""
    lowered = error.lower()
    patterns = (
        ("unsupported type", "unsupported_type"),
        ("invalid isin", "invalid_isin"),
        ("headers", "invalid_schema"),
        ("header", "invalid_schema"),
        ("mapping", "mapping_resolution"),
        ("placeholder", "placeholder_value"),
        ("configured byte limit", "file_too_large"),
        ("transaction-row limit", "too_many_rows"),
        ("conflicts with the ledger payload", "identity_conflict"),
        ("needs_review", "identity_ambiguity"),
        ("ambiguous", "ambiguous_target"),
        ("portfolio performance is open", "portfolio_performance_open"),
        ("official portfolio performance engine failed", "engine_failure"),
        ("symlink", "unsafe_path"),
        ("escapes the managed root", "unsafe_path"),
        ("currency", "currency_validation"),
        ("fees", "fees_validation"),
        ("taxes", "taxes_validation"),
        ("shares", "shares_validation"),
        ("value", "value_validation"),
        ("date", "date_validation"),
    )
    code = next((value for marker, value in patterns if marker in lowered), "workflow_error")
    line = re.search(r"\bLine (\d+)\b", error)
    field = next(
        (
            name
            for name in (
                "SchemaVersion",
                "PortfolioKey",
                "SourceSystem",
                "SourceDocumentId",
                "ExternalTransactionId",
                "Date",
                "Type",
                "Transaction Currency",
                "SecurityKey",
                "ISIN",
                "Security Name",
                "Shares",
                "Fees",
                "Taxes",
                "Value",
                "CashAccountKey",
                "Cash Account",
                "SecuritiesAccountKey",
                "Securities Account",
            )
            if name.lower() in lowered
        ),
        "",
    )
    parts = [f"code={code}"]
    if line:
        parts.append(f"line={line.group(1)}")
    if field:
        parts.append(f"field={field}")
    return ";".join(parts)


def write_log(
    config: RuntimeConfig,
    event: str,
    *,
    csv_path: Path | None = None,
    csv_sha256: str = "",
    outcome: str = "",
    backup_path: Path | None = None,
    error: str = "",
    **details: Any,
) -> None:
    record: dict[str, Any] = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": event,
        "csv_path": str(csv_path) if csv_path else "",
        "csv_sha256": csv_sha256,
        "outcome": outcome,
        "xml_path": str(config.xml_file),
        "backup_path": str(backup_path) if backup_path else "",
        "error": _audit_error_summary(error),
    }
    record.update(details)
    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    prior = b""
    if config.log_file.is_file():
        prior = config.log_file.read_bytes()
        if prior and not prior.endswith(b"\n"):
            raise ImportWorkflowError("Audit log has an incomplete tail")
        for line in prior.splitlines():
            try:
                json.loads(line)
            except (UnicodeError, json.JSONDecodeError) as error:
                raise ImportWorkflowError("Audit log contains an invalid event") from error
    encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=config.log_file.parent,
            prefix=f".{config.log_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(prior)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config.log_file)
        temporary = None
        _fsync_directory(config.log_file.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_report(
    config: RuntimeConfig,
    event: str,
    *,
    csv_path: Path | None = None,
    csv_sha256: str = "",
    outcome: str = "",
    error: str = "",
    **details: Any,
) -> Path:
    """Write one privacy-minimal, durable per-file outcome report."""
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": event,
        "csv_file": csv_path.name if csv_path else "",
        "csv_sha256": csv_sha256,
        "outcome": outcome,
        "xml_sha256": (
            sha256_file(config.xml_file) if config.xml_file.is_file() else ""
        ),
        "error": _audit_error_summary(error),
    }
    record.update(details)
    stem = csv_sha256[:12] if csv_sha256 else "unhashed"
    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
    destination = unique_destination(
        config.reports_dir,
        f"{timestamp}-{stem}-{outcome or event}.json",
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config.reports_dir,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(record, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
        _fsync_directory(config.reports_dir)
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "generation": 0,
        "processed_sha256": [],
        "files": {},
        "identities": {},
        "legacy_payloads": {},
        "economic_payloads": {},
        "legacy_history_complete": True,
    }


def _state_checksum(state: dict[str, Any]) -> str:
    body = {key: value for key, value in state.items() if key != "checksum"}
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_state(path: Path) -> dict[str, Any] | None:
    try:
        state = _load_json_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    version = state.get("version")
    if version == 3:
        migrated = empty_state()
        processed = state.get("processed_sha256", [])
        if not isinstance(processed, list) or not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value)
            for value in processed
        ):
            return None
        migrated["processed_sha256"] = [value.lower() for value in processed]
        # V3 recorded only whole-file hashes. Without row payloads, a
        # reformatted missing-ID document cannot be proved novel, so V1 input
        # must remain review-only after migration unless it is an exact hash.
        migrated["legacy_history_complete"] = False
        for key in (
            "xml_file",
            "xml_sha256",
            "engine_summary",
            "logical_transactions",
            "updated_at",
        ):
            if key in state:
                migrated[key] = state[key]
        return migrated
    if version != STATE_VERSION:
        return None
    if not isinstance(state.get("generation"), int) or state["generation"] < 0:
        return None
    expected = state.get("checksum")
    if not isinstance(expected, str) or expected != _state_checksum(state):
        return None
    for key, expected_type in (
        ("processed_sha256", list),
        ("files", dict),
        ("identities", dict),
        ("legacy_payloads", dict),
        ("economic_payloads", dict),
    ):
        if not isinstance(state.get(key), expected_type):
            return None
    if not isinstance(state.get("legacy_history_complete"), bool):
        return None
    return state


def load_state(config: RuntimeConfig) -> dict[str, Any]:
    temporary_pattern = f".{config.state_file.name}.*.tmp"
    temporary_files = sorted(
        config.state_file.parent.glob(temporary_pattern),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if config.state_file.exists():
        state = _validated_state(config.state_file)
        if state is not None:
            return state
        try:
            readable = _load_json_text(config.state_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            readable = None
        if isinstance(readable, dict):
            version = readable.get("version")
            if version not in {3, STATE_VERSION}:
                raise ImportWorkflowError(
                    f"State ledger uses unsupported version {version!r}: {config.state_file}"
                )
            if version == 3:
                raise ImportWorkflowError(
                    f"Legacy state ledger is malformed: {config.state_file}"
                )
    else:
        state = None

    recoverable = [
        (candidate, recovered)
        for candidate in temporary_files
        if (recovered := _validated_state(candidate)) is not None
    ]
    if recoverable:
        candidate, recovered = max(
            recoverable,
            key=lambda item: (item[1]["generation"], item[0].stat().st_mtime_ns),
        )
        os.replace(candidate, config.state_file)
        return recovered

    if not config.state_file.exists():
        return empty_state()
    raise ImportWorkflowError(
        f"State ledger is corrupt or uses an unsupported version: {config.state_file}"
    )


def atomic_write_state(config: RuntimeConfig, state: dict[str, Any]) -> None:
    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    serialized = dict(state)
    serialized["version"] = STATE_VERSION
    serialized["generation"] = int(serialized.get("generation", 0)) + 1
    serialized["checksum"] = _state_checksum(serialized)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config.state_file.parent,
            prefix=f".{config.state_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(serialized, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config.state_file)
        temporary = None
        if os.name != "nt":
            directory_fd = os.open(config.state_file.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _state_contains_commit(
    observed: dict[str, Any], expected: dict[str, Any]
) -> bool:
    """Recognize a commit even if its post-rename durability barrier raised."""
    if "pending" in observed:
        return False
    return all(
        observed.get(key) == expected.get(key)
        for key in (
            "processed_sha256",
            "files",
            "identities",
            "legacy_payloads",
            "economic_payloads",
            "legacy_history_complete",
            "xml_file",
            "xml_sha256",
            "engine_summary",
            "logical_transactions",
        )
    )


def validate_committed_xml_state(
    config: RuntimeConfig, state: dict[str, Any]
) -> None:
    """Fail closed when the managed XML and accepted ledger are not a pair."""
    if "pending" in state:
        return
    expected = state.get("xml_sha256")
    accepted = state.get("processed_sha256", [])
    if expected is None and not accepted:
        return
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ImportNeedsReview(
            "Committed ledger has no valid managed XML hash; restore a paired XML/state backup"
        )
    observed = sha256_file(config.xml_file) if config.xml_file.is_file() else ""
    if observed != expected:
        raise ImportNeedsReview(
            "Managed XML does not match the committed ledger; restore the paired XML/state "
            "backup before importing or skipping CSV files"
        )


def _is_placeholder_account(value: str) -> bool:
    normalized = re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")
    return any(
        marker in normalized
        for marker in (
            "CHANGE_ME",
            "YOUR_ACCOUNT",
            "ACCOUNT_NAME",
            "PLACEHOLDER",
            "REPLACE_ME",
            "PUT_ACCOUNT_HERE",
        )
    )


def _normalized_text(value: str, *, field: str, line_number: int) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    if any(ord(character) < 32 and character not in "\r\n\t" for character in normalized):
        raise CSVValidationError(
            f"Line {line_number}: {field} contains a control character"
        )
    return normalized


def _stable_component(value: str, *, field: str, line_number: int) -> str:
    normalized = _normalized_text(value, field=field, line_number=line_number)
    if not normalized:
        raise CSVValidationError(f"Line {line_number}: {field} is required")
    if any(ord(character) < 32 for character in normalized):
        raise CSVValidationError(
            f"Line {line_number}: {field} contains forbidden control whitespace"
        )
    if _is_placeholder_account(normalized):
        raise CSVValidationError(f"Line {line_number}: {field} contains a placeholder")
    return normalized


def _legacy_name_key(value: str) -> str:
    return "legacy-name:" + " ".join(
        unicodedata.normalize("NFC", value).split()
    ).casefold()


def _parse_mapping_target(
    item: Any,
    *,
    section: str,
    position: int,
) -> MappingTarget:
    if not isinstance(item, dict):
        raise ConfigurationError(f"account mapping {section}[{position}] must be an object")
    key = unicodedata.normalize("NFC", str(item.get("key", ""))).strip()
    target_uuid = str(item.get("uuid", "")).strip().lower()
    name = unicodedata.normalize("NFC", str(item.get("name", ""))).strip()
    currency = str(item.get("currency", "")).strip().upper()
    isin = str(item.get("isin", "")).strip().upper()
    reference_key = unicodedata.normalize(
        "NFC",
        str(item.get("reference_cash_account_key", "")),
    ).strip()
    if not key:
        raise ConfigurationError(f"account mapping {section}[{position}].key is required")
    if _is_placeholder_account(key) or (name and _is_placeholder_account(name)):
        raise ConfigurationError(f"account mapping {section}[{position}] contains a placeholder")
    if not target_uuid and not name and not isin:
        raise ConfigurationError(
            f"account mapping {section}[{position}] needs uuid, exact name, or ISIN"
        )
    if section in {"cash_accounts", "securities_accounts"} and not target_uuid and not name:
        raise ConfigurationError(
            f"account mapping {section}[{position}] needs uuid or exact name"
        )
    if target_uuid:
        try:
            target_uuid = str(uuid.UUID(target_uuid))
        except ValueError as error:
            raise ConfigurationError(
                f"account mapping {section}[{position}].uuid is invalid"
            ) from error
    if currency and not re.fullmatch(r"[A-Z]{3}", currency):
        raise ConfigurationError(
            f"account mapping {section}[{position}].currency must be three uppercase letters"
        )
    if isin and not _valid_isin(isin):
        raise ConfigurationError(f"account mapping {section}[{position}].isin is invalid")
    return MappingTarget(
        key=key,
        uuid=target_uuid,
        name=name,
        currency=currency,
        isin=isin,
        reference_cash_account_key=reference_key,
    )


def _mapping_section(raw: dict[str, Any], section: str) -> tuple[MappingTarget, ...]:
    items = raw.get(section, [])
    if not isinstance(items, list):
        raise ConfigurationError(f"account mapping {section} must be a list")
    targets = tuple(
        _parse_mapping_target(item, section=section, position=position)
        for position, item in enumerate(items, start=1)
    )
    seen_keys: set[str] = set()
    for target in targets:
        normalized_key = unicodedata.normalize("NFC", target.key)
        if normalized_key in seen_keys:
            raise ConfigurationError(f"account mapping {section} has duplicate key {target.key!r}")
        seen_keys.add(normalized_key)
    return targets


def load_account_mapping(config: RuntimeConfig) -> AccountMapping:
    if not config.account_mapping_file.exists():
        return AccountMapping()
    try:
        raw = _load_json_text(config.account_mapping_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ConfigurationError(
            f"Invalid account mapping file {config.account_mapping_file}: {error}"
        ) from error
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ConfigurationError("account mapping version must be 1")
    return AccountMapping(
        cash_accounts=_mapping_section(raw, "cash_accounts"),
        securities_accounts=_mapping_section(raw, "securities_accounts"),
        securities=_mapping_section(raw, "securities"),
    )


def _resolve_mapping_target(
    targets: tuple[MappingTarget, ...],
    *,
    key: str,
    name: str,
    currency: str,
    line_number: int,
    label: str,
    require_stable_key: bool,
    isin: str = "",
) -> MappingTarget:
    normalized_key = _normalized_text(key, field=f"{label} key", line_number=line_number)
    normalized_name = _normalized_text(name, field=label, line_number=line_number)
    if normalized_key and _is_placeholder_account(normalized_key):
        raise CSVValidationError(f"Line {line_number}: {label} key contains a placeholder")
    if normalized_name and _is_placeholder_account(normalized_name):
        raise CSVValidationError(f"Line {line_number}: {label} contains a placeholder")
    if require_stable_key and not normalized_key:
        raise CSVValidationError(f"Line {line_number}: {label} key is required for V2")

    matches: list[MappingTarget]
    if normalized_key:
        matches = [target for target in targets if target.key == normalized_key]
    elif isin:
        matches = [target for target in targets if target.isin == isin]
    elif normalized_name:
        matches = [target for target in targets if target.name == normalized_name]
    else:
        matches = []

    if targets:
        if len(matches) != 1:
            reason = "ambiguous" if len(matches) > 1 else "missing"
            raise CSVValidationError(
                f"Line {line_number}: {label} mapping is {reason}"
            )
        target = matches[0]
        if (
            not normalized_key
            and normalized_name
            and target.name
            and normalized_name != target.name
        ):
            raise CSVValidationError(
                f"Line {line_number}: {label} name conflicts with mapping key {target.key!r}"
            )
        if isin and target.isin and isin != target.isin:
            raise CSVValidationError(
                f"Line {line_number}: {label} ISIN conflicts with mapping key {target.key!r}"
            )
        if target.currency and currency != target.currency:
            raise CSVValidationError(
                f"Line {line_number}: {label} currency {currency} conflicts with mapping {target.currency}"
            )
        return target

    if require_stable_key:
        raise CSVValidationError(
            f"Line {line_number}: {label} key {normalized_key!r} has no mapping"
        )
    if not normalized_name:
        raise CSVValidationError(f"Line {line_number}: {label} is required")
    return MappingTarget(
        key=_legacy_name_key(normalized_name),
        uuid="",
        name=normalized_name,
        currency=currency,
        isin=isin,
    )


def _normalize_numeric_text(value: str, profile: str, *, line_number: int, field: str) -> str:
    if "\u00a0" in value or " " in value or "\t" in value:
        raise CSVValidationError(f"Line {line_number}: {field} contains unsupported spacing")
    if profile == "plain":
        pattern = r"[+-]?\d+(?:\.\d+)?"
        normalized = value
    elif profile == "en_US":
        pattern = r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?"
        normalized = value.replace(",", "")
    else:
        pattern = r"[+-]?(?:\d+|\d{1,3}(?:\.\d{3})+)(?:,\d+)?"
        normalized = value.replace(".", "").replace(",", ".")
    if not re.fullmatch(pattern, value):
        raise CSVValidationError(
            f"Line {line_number}: {field} is not valid for decimal profile {profile}"
        )
    return normalized


def _parse_decimal(
    value: str,
    *,
    line_number: int,
    field: str,
    required: bool,
    positive: bool = False,
    scale: int = 2,
    profile: str = "plain",
) -> Decimal | None:
    if not value:
        if required:
            raise CSVValidationError(f"Line {line_number}: {field} is required")
        return None
    try:
        parsed = Decimal(
            _normalize_numeric_text(
                value,
                profile,
                line_number=line_number,
                field=field,
            )
        )
    except InvalidOperation as error:
        raise CSVValidationError(
            f"Line {line_number}: {field} must be numeric"
        ) from error
    if not parsed.is_finite():
        raise CSVValidationError(f"Line {line_number}: {field} must be finite")
    if positive and parsed <= 0:
        raise CSVValidationError(
            f"Line {line_number}: {field} must be greater than zero"
        )
    if not positive and parsed < 0:
        raise CSVValidationError(
            f"Line {line_number}: {field} must not be negative"
        )
    quantum = Decimal(1).scaleb(-scale)
    try:
        if parsed != parsed.quantize(quantum):
            raise CSVValidationError(
                f"Line {line_number}: {field} supports at most {scale} decimal places"
            )
    except InvalidOperation as error:
        raise CSVValidationError(
            f"Line {line_number}: {field} has invalid precision"
        ) from error
    return parsed


def _decimal_text(value: Decimal | None) -> str:
    if value is None or value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _valid_isin(value: str) -> bool:
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", value):
        return False
    invalid_codes = {
        "DU",
        "EV",
        "HF",
        "HS",
        "QS",
        "QT",
        "QU",
        "QY",
        "TE",
        "XF",
        "XX",
        "ZZ",
    }
    if value[:2] in invalid_codes:
        return False
    digits = "".join(str(int(char, 36)) for char in value)
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = int(char) * (2 if index % 2 else 1)
        total += number // 10 + number % 10
    return total % 10 == 0


def _parse_date(value: str, config: RuntimeConfig, *, line_number: int) -> str:
    for date_format in config.date_formats:
        try:
            parsed = datetime.strptime(value, date_format)
        except ValueError:
            continue
        if parsed.strftime(date_format) == value:
            return parsed.strftime("%Y-%m-%d")
    expected = " or ".join(
        "YYYY-MM-DD" if item == "%Y-%m-%d" else "DD.MM.YYYY"
        for item in config.date_formats
    )
    raise CSVValidationError(f"Line {line_number}: Date must use {expected}")


def _read_csv_table(
    path: Path,
    *,
    max_file_bytes: int,
    max_rows: int,
) -> tuple[tuple[str, ...], list[tuple[int, list[str]]]]:
    if path.is_symlink():
        raise CSVValidationError(f"Symlink inputs are not allowed: {path}")
    if not path.is_file() or path.suffix.lower() != ".csv":
        raise CSVValidationError(f"Expected a regular .csv file: {path}")
    size = path.stat().st_size
    if size > max_file_bytes:
        raise CSVValidationError(
            f"CSV exceeds the configured byte limit ({max_file_bytes})"
        )
    records: list[tuple[int, list[str]]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream, strict=True)
            try:
                headers = tuple(next(reader))
            except StopIteration as error:
                raise CSVValidationError("CSV is empty") from error
            if len(headers) != len(set(headers)):
                raise CSVValidationError("CSV headers contain duplicates")
            for line_number, raw in enumerate(reader, start=2):
                if not raw or not any(cell.strip() for cell in raw):
                    continue
                if len(records) >= max_rows:
                    raise CSVValidationError(
                        f"CSV exceeds the configured transaction-row limit ({max_rows})"
                    )
                records.append((line_number, raw))
    except (UnicodeError, csv.Error) as error:
        raise CSVValidationError(f"CSV encoding or syntax error: {error}") from error
    if not records:
        raise CSVValidationError("CSV has a header but no transaction rows")
    return headers, records


def read_canonical_records(path: Path, config: RuntimeConfig) -> list[CanonicalRecord]:
    if config.csv_encoding.lower().replace("_", "-") not in ("utf-8", "utf-8-sig"):
        raise CSVValidationError("Only UTF-8 or UTF-8 with BOM is supported")
    headers, records = _read_csv_table(
        path,
        max_file_bytes=config.csv_max_file_bytes,
        max_rows=config.csv_max_rows,
    )
    if headers == CSV_HEADERS:
        schema_version = "1"
    elif headers == CSV_V2_HEADERS:
        schema_version = "2"
    else:
        raise CSVValidationError(
            "CSV headers must exactly match legacy V1 or canonical V2"
        )

    mapping = load_account_mapping(config)
    answer: list[CanonicalRecord] = []
    for occurrence, (line_number, raw) in enumerate(records, start=1):
        if len(raw) != len(headers):
            raise CSVValidationError(
                f"Line {line_number}: expected {len(headers)} columns, got {len(raw)}"
            )
        row = {
            header: _normalized_text(value, field=header, line_number=line_number)
            for header, value in zip(headers, raw)
        }
        for field, value in row.items():
            if value == "-":
                raise CSVValidationError(
                    f"Line {line_number}: use an empty value instead of '-' in {field}"
                )

        if schema_version == "2":
            if row["SchemaVersion"] != "2":
                raise CSVValidationError(
                    f"Line {line_number}: SchemaVersion must be 2"
                )
            portfolio_key = _stable_component(
                row["PortfolioKey"], field="PortfolioKey", line_number=line_number
            )
            if portfolio_key != config.managed_portfolio_key:
                raise CSVValidationError(
                    f"Line {line_number}: PortfolioKey does not match the managed portfolio"
                )
            source_system = _stable_component(
                row["SourceSystem"], field="SourceSystem", line_number=line_number
            )
            source_document_id = _stable_component(
                row["SourceDocumentId"],
                field="SourceDocumentId",
                line_number=line_number,
            )
            external_id = _stable_component(
                row["ExternalTransactionId"],
                field="ExternalTransactionId",
                line_number=line_number,
            )
            identity_provenance = "external-v2"
        else:
            portfolio_key = config.managed_portfolio_key
            source_system = "legacy-v1"
            source_document_id = ""
            external_id = ""
            identity_provenance = "file-sha256+occurrence"

        date = _parse_date(row["Date"], config, line_number=line_number)
        transaction_type = row["Type"]
        if transaction_type not in config.supported_types:
            raise CSVValidationError(
                f"Line {line_number}: unsupported Type {transaction_type!r}"
            )
        currency = row["Transaction Currency"].upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise CSVValidationError(
                f"Line {line_number}: Transaction Currency must be three uppercase letters"
            )
        isin = row["ISIN"].upper()
        if isin and not _valid_isin(isin):
            raise CSVValidationError(f"Line {line_number}: invalid ISIN {isin!r}")

        value = _parse_decimal(
            row["Value"],
            line_number=line_number,
            field="Value",
            required=True,
            positive=True,
            profile=config.decimal_profile,
        )
        fees = _parse_decimal(
            row["Fees"],
            line_number=line_number,
            field="Fees",
            required=False,
            profile=config.decimal_profile,
        ) or Decimal(0)
        taxes = _parse_decimal(
            row["Taxes"],
            line_number=line_number,
            field="Taxes",
            required=False,
            profile=config.decimal_profile,
        ) or Decimal(0)
        if fees < 0:
            raise CSVValidationError(
                f"Line {line_number}: Fees must be a positive magnitude or zero"
            )
        if taxes < 0:
            raise CSVValidationError(
                f"Line {line_number}: Taxes must be a positive magnitude or zero"
            )
        shares = _parse_decimal(
            row["Shares"],
            line_number=line_number,
            field="Shares",
            required=transaction_type in {"Buy", "Sell"},
            positive=True,
            scale=8,
            profile=config.decimal_profile,
        )

        security_key_input = row.get("SecurityKey", "")
        cash_key_input = row.get("CashAccountKey", "")
        portfolio_key_input = row.get("SecuritiesAccountKey", "")
        cash_name = row["Cash Account"]
        securities_name = row["Securities Account"]
        security_name = row["Security Name"]
        if schema_version == "1":
            cash_name = cash_name or f"Cash {currency}"
            if transaction_type in SECURITY_TYPES:
                securities_name = securities_name or f"Portfolio {currency}"

        if transaction_type not in SECURITY_TYPES:
            security_fields = (
                security_key_input,
                isin,
                security_name,
                row["Shares"],
                portfolio_key_input,
                securities_name,
            )
            if any(security_fields):
                raise CSVValidationError(
                    f"Line {line_number}: {transaction_type} security fields must be empty"
                )
            if fees or taxes:
                raise CSVValidationError(
                    f"Line {line_number}: {transaction_type} Fees and Taxes must be empty or zero"
                )
        elif schema_version == "1" and not security_name:
            raise CSVValidationError(
                f"Line {line_number}: Security Name is required for {transaction_type}"
            )
        if transaction_type == "Buy" and value <= fees + taxes:
            raise CSVValidationError(
                f"Line {line_number}: Buy Value must exceed Fees plus Taxes"
            )

        cash_target = _resolve_mapping_target(
            mapping.cash_accounts,
            key=cash_key_input,
            name=cash_name,
            currency=currency,
            line_number=line_number,
            label="Cash Account",
            require_stable_key=schema_version == "2",
        )

        security_target = MappingTarget("", "", "", currency)
        portfolio_target = MappingTarget("", "", "", currency)
        if transaction_type in SECURITY_TYPES:
            security_target = _resolve_mapping_target(
                mapping.securities,
                key=security_key_input,
                name=security_name,
                currency=currency,
                line_number=line_number,
                label="Security",
                require_stable_key=schema_version == "2",
                isin=isin,
            )
            portfolio_target = _resolve_mapping_target(
                mapping.securities_accounts,
                key=portfolio_key_input,
                name=securities_name,
                currency=currency,
                line_number=line_number,
                label="Securities Account",
                require_stable_key=schema_version == "2",
            )
            if (
                portfolio_target.reference_cash_account_key
                and portfolio_target.reference_cash_account_key != cash_target.key
            ):
                references = [
                    target
                    for target in mapping.cash_accounts
                    if target.key == portfolio_target.reference_cash_account_key
                ]
                same_target = (
                    len(references) == 1
                    and (
                        references[0].uuid or f"name:{references[0].name}"
                    )
                    == (cash_target.uuid or f"name:{cash_target.name}")
                )
                if not same_target:
                    raise CSVValidationError(
                        f"Line {line_number}: Securities Account is mapped to a different Cash Account"
                    )

        answer.append(
            CanonicalRecord(
                schema_version=schema_version,
                identity_provenance=identity_provenance,
                portfolio_key=portfolio_key,
                source_system=source_system,
                source_document_id=source_document_id,
                external_transaction_id=external_id,
                occurrence=occurrence,
                line_number=line_number,
                date=date,
                transaction_type=transaction_type,
                currency=currency,
                security_key=security_target.key,
                security_uuid=security_target.uuid,
                isin=security_target.isin or isin,
                security_name=security_target.name or security_name,
                shares=_decimal_text(shares),
                fees=_decimal_text(fees),
                taxes=_decimal_text(taxes),
                value=_decimal_text(value),
                cash_account_key=cash_target.key,
                cash_account_uuid=cash_target.uuid,
                cash_account_name=cash_target.name,
                securities_account_key=portfolio_target.key,
                securities_account_uuid=portfolio_target.uuid,
                securities_account_name=portfolio_target.name,
                note=row["Note"],
            )
        )
    return answer


def read_validated_rows(path: Path, config: RuntimeConfig) -> list[dict[str, str]]:
    return [
        dict(zip(CSV_HEADERS, record.engine_row("")[: len(CSV_HEADERS)]))
        for record in read_canonical_records(path, config)
    ]


def validate_csv(path: Path, config: RuntimeConfig) -> int:
    return len(read_canonical_records(path, config))


def discover_csv_files(directory: Path) -> list[Path]:
    return sorted(
        (path for path in directory.iterdir() if path.suffix.lower() == ".csv"),
        key=lambda path: (path.lstat().st_mtime_ns, path.name.casefold()),
    )


def wait_until_stable(path: Path, config: RuntimeConfig) -> bool:
    previous: tuple[int, int] | None = None
    stable = 0
    for observation in range(config.stable_observations):
        try:
            current_stat = path.lstat()
        except OSError:
            return False
        current = (current_stat.st_size, current_stat.st_mtime_ns)
        if current == previous:
            stable += 1
        else:
            previous = current
            stable = 1
        if observation + 1 < config.stable_observations:
            time.sleep(config.stability_check_interval_seconds)
    return stable >= config.stable_observations


def unique_destination(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    counter = 2
    while candidate.exists() or candidate.is_symlink():
        source = Path(filename)
        candidate = directory / f"{source.stem}.{counter}{source.suffix}"
        counter += 1
    return candidate


def _move_csv_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.stat().st_dev != destination.parent.stat().st_dev:
        raise ImportWorkflowError(
            f"CSV state directories must share one filesystem: {source.parent} and {destination.parent}"
        )
    source_parent = source.parent
    os.replace(source, destination)
    try:
        _fsync_directory(destination.parent)
        if source_parent != destination.parent:
            _fsync_directory(source_parent)
    except Exception as barrier_error:
        try:
            os.replace(destination, source)
        except Exception as reverse_error:
            raise CsvMoveUncertain(
                f"CSV move reached {destination}, durability barrier failed, and reverse move failed: {reverse_error}",
                source=source,
                destination=destination,
            ) from barrier_error
        for directory in {source_parent, destination.parent}:
            try:
                _fsync_directory(directory)
            except Exception:
                pass
        raise


def claim_csv(
    source: Path,
    processing_dir: Path,
    *,
    max_file_bytes: int,
) -> Path:
    """Detach stable inbox bytes from any producer file descriptor."""
    destination = unique_destination(processing_dir, source.name)
    processing_dir.mkdir(parents=True, exist_ok=True)
    if source.stat().st_dev != processing_dir.stat().st_dev:
        raise ImportDeferred(
            "CSV inbox and processing directory must share one filesystem"
        )
    if source.stat().st_size > max_file_bytes:
        raise CSVValidationError(
            f"CSV exceeds the configured byte limit ({max_file_bytes})"
        )
    source_hash_before = sha256_file_bounded(source, max_file_bytes)
    temporary: Path | None = None
    try:
        with source.open("rb") as input_stream, tempfile.NamedTemporaryFile(
            mode="wb",
            dir=processing_dir,
            prefix=f".{destination.name}.",
            suffix=".claim.tmp",
            delete=False,
        ) as output_stream:
            temporary = Path(output_stream.name)
            copied = 0
            while True:
                block = input_stream.read(
                    min(1024 * 1024, max_file_bytes - copied + 1)
                )
                if not block:
                    break
                copied += len(block)
                if copied > max_file_bytes:
                    raise CSVValidationError(
                        f"CSV exceeds the configured byte limit ({max_file_bytes})"
                    )
                output_stream.write(block)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        source_hash_after = sha256_file_bounded(source, max_file_bytes)
        snapshot_hash = sha256_file_bounded(temporary, max_file_bytes)
        if not (
            source_hash_before == source_hash_after == snapshot_hash
        ):
            raise ImportDeferred(
                "CSV changed while its private processing snapshot was created; "
                "it remains queued"
            )
        os.replace(temporary, destination)
        temporary = None
        try:
            _fsync_directory(processing_dir)
        except Exception as publish_barrier_error:
            try:
                destination.unlink(missing_ok=True)
                _fsync_directory(processing_dir)
            except Exception as cleanup_error:
                raise CsvMoveUncertain(
                    "CSV snapshot rename reached processing, its durability barrier failed, "
                    f"and snapshot reversal failed: {cleanup_error}",
                    source=source,
                    destination=destination,
                ) from publish_barrier_error
            raise ImportDeferred(
                "CSV snapshot durability barrier failed; the inbox file remains queued"
            ) from publish_barrier_error
        try:
            source.unlink()
        except Exception as unlink_error:
            try:
                destination.unlink(missing_ok=True)
                _fsync_directory(processing_dir)
            except Exception as cleanup_error:
                raise CsvMoveUncertain(
                    "CSV snapshot was published but inbox cleanup and snapshot reversal "
                    f"failed: {cleanup_error}",
                    source=source,
                    destination=destination,
                ) from unlink_error
            raise ImportDeferred(
                f"CSV snapshot could not release the inbox name and remains queued: {unlink_error}"
            ) from unlink_error
        try:
            _fsync_directory(source.parent)
        except Exception as unlink_barrier_error:
            raise CsvMoveUncertain(
                "CSV inbox unlink reached disk state but its durability barrier failed; "
                "the verified processing snapshot is retained",
                source=source,
                destination=destination,
            ) from unlink_barrier_error
    except ImportWorkflowError:
        raise
    except OSError as error:
        raise ImportDeferred(
            f"CSV could not be claimed as a private snapshot and remains queued: {error}"
        ) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def route_unchanged_csv(source: Path, directory: Path, expected_hash: str) -> Path:
    destination = unique_destination(directory, source.name)
    _move_csv_atomic(source, destination)
    actual_hash = sha256_file(destination)
    if actual_hash != expected_hash:
        try:
            _move_csv_atomic(destination, source)
        except Exception as restore_error:
            raise ImportWorkflowError(
                f"CSV hash changed while routing to {destination}; restore failed: {restore_error}"
            ) from restore_error
        raise ImportWorkflowError(
            f"CSV hash changed while routing: expected {expected_hash}, got {actual_hash}"
        )
    return destination


def history_hashes(config: RuntimeConfig) -> list[str]:
    hashes = {
        sha256_file(path)
        for path in discover_csv_files(config.processed_dir)
        if path.is_file() and not path.is_symlink()
    }
    return sorted(hashes)


def accepted_hashes(config: RuntimeConfig) -> list[str]:
    """Return accepted CSV hashes from the authoritative durable ledger."""
    hashes: set[str] = set()
    stored = load_state(config).get("processed_sha256", [])
    if isinstance(stored, list):
        hashes.update(
            value.lower()
            for value in stored
            if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value)
        )
    return sorted(hashes)


def assess_identities(
    records: list[CanonicalRecord],
    state: dict[str, Any],
    file_sha256: str,
) -> IdentityDecision:
    if file_sha256 in set(state.get("processed_sha256", [])):
        return IdentityDecision("skip", (), tuple(records), "exact file SHA-256 already accepted")

    if not records:
        raise CSVValidationError("CSV contains no canonical records")
    schema_versions = {record.schema_version for record in records}
    if len(schema_versions) != 1:
        raise CSVValidationError("A CSV document cannot mix schema versions")

    identities = state.get("identities", {})
    economic_payloads = state.get(
        "economic_payloads",
        state.get("legacy_payloads", {}),
    )
    if not isinstance(identities, dict) or not isinstance(economic_payloads, dict):
        raise ImportWorkflowError("State ledger identity indexes are invalid")

    if records[0].schema_version == "1":
        if not state.get("legacy_history_complete", True):
            return IdentityDecision(
                "needs_review",
                (),
                (),
                "legacy V3 history has no row-level identities",
            )
        ambiguous = sorted(
            {
                record.payload_sha256()
                for record in records
                if record.payload_sha256() in economic_payloads
            }
        )
        if ambiguous:
            return IdentityDecision(
                "needs_review",
                (),
                (),
                f"legacy economic payload already exists ({len(ambiguous)} match(es))",
            )
        return IdentityDecision("add", tuple(records), ())

    local: dict[str, str] = {}
    new_records: list[CanonicalRecord] = []
    skipped_records: list[CanonicalRecord] = []
    for record in records:
        identity_key = record.identity_key(file_sha256)
        payload_sha = record.payload_sha256()
        prior_local = local.get(identity_key)
        if prior_local is not None:
            if prior_local != payload_sha:
                return IdentityDecision(
                    "reject",
                    (),
                    (),
                    f"Line {record.line_number}: duplicate external identity has a changed payload",
                )
            skipped_records.append(record)
            continue
        local[identity_key] = payload_sha
        prior = identities.get(identity_key)
        if prior is None:
            new_records.append(record)
            continue
        if not isinstance(prior, dict) or prior.get("payload_sha256") != payload_sha:
            return IdentityDecision(
                "reject",
                (),
                (),
                f"Line {record.line_number}: external identity conflicts with the ledger payload",
            )
        skipped_records.append(record)
    outcome = "add" if new_records else "skip"
    return IdentityDecision(outcome, tuple(new_records), tuple(skipped_records))


def commit_identity_ledger(
    state: dict[str, Any],
    records: Iterable[CanonicalRecord],
    *,
    file_sha256: str,
    xml_sha256: str,
) -> dict[str, Any]:
    committed = dict(state)
    processed = set(committed.get("processed_sha256", []))
    processed.add(file_sha256)
    committed["processed_sha256"] = sorted(processed)
    files = dict(committed.get("files", {}))
    identities = dict(committed.get("identities", {}))
    legacy_payloads = {
        key: list(value)
        for key, value in committed.get("legacy_payloads", {}).items()
        if isinstance(value, list)
    }
    economic_payloads = {
        key: list(value)
        for key, value in committed.get("economic_payloads", {}).items()
        if isinstance(value, list)
    }
    identity_keys: list[str] = []
    payloads: list[str] = []
    source_document_ids: list[str] = []
    schema_version = ""
    for record in records:
        schema_version = record.schema_version
        identity_key = record.identity_key(file_sha256)
        payload_sha = record.payload_sha256()
        identity_keys.append(identity_key)
        payloads.append(payload_sha)
        if record.source_document_id:
            source_document_ids.append(record.source_document_id)
        if record.schema_version == "2":
            prior_identity = identities.get(identity_key, {})
            prior_documents: set[str] = set()
            if isinstance(prior_identity, dict):
                prior_document = prior_identity.get("source_document_id")
                if isinstance(prior_document, str) and prior_document:
                    prior_documents.add(prior_document)
                stored_documents = prior_identity.get("source_document_ids", [])
                if isinstance(stored_documents, list):
                    prior_documents.update(
                        value
                        for value in stored_documents
                        if isinstance(value, str) and value
                    )
            prior_documents.add(record.source_document_id)
            identities[identity_key] = {
                "payload_sha256": payload_sha,
                "portfolio_key": record.portfolio_key,
                "source_system": record.source_system,
                "source_document_id": (
                    prior_identity.get("source_document_id", record.source_document_id)
                    if isinstance(prior_identity, dict)
                    else record.source_document_id
                ),
                "source_document_ids": sorted(prior_documents),
                "external_transaction_id": record.external_transaction_id,
            }
        else:
            owners = set(legacy_payloads.get(payload_sha, []))
            owners.add(file_sha256)
            legacy_payloads[payload_sha] = sorted(owners)
        economic_owners = set(economic_payloads.get(payload_sha, []))
        economic_owners.add(file_sha256)
        economic_payloads[payload_sha] = sorted(economic_owners)
    files[file_sha256] = {
        "schema_version": schema_version,
        "identity_keys": identity_keys,
        "payload_sha256": payloads,
        "source_document_ids": sorted(set(source_document_ids)),
        "xml_sha256": xml_sha256,
    }
    committed["files"] = files
    committed["identities"] = identities
    committed["legacy_payloads"] = legacy_payloads
    committed["economic_payloads"] = economic_payloads
    committed.pop("pending", None)
    return committed


def reconcile_pending_state(
    config: RuntimeConfig,
    state: dict[str, Any],
    *,
    current_file_sha256: str = "",
) -> dict[str, Any]:
    pending = state.get("pending")
    if pending is None:
        return state
    if not isinstance(pending, dict) or not isinstance(pending.get("xml_sha256_before"), str):
        raise ImportWorkflowError("State ledger contains an invalid pending journal")
    current_hash = sha256_file(config.xml_file) if config.xml_file.is_file() else ""
    if current_hash != pending["xml_sha256_before"]:
        pending_file = pending.get("file_sha256")
        if current_file_sha256 and pending_file != current_file_sha256:
            raise ImportDeferred(
                "A different import has an unresolved pending ledger journal; "
                "this CSV remains queued"
            )
        raise ImportNeedsReview(
            "Pending ledger journal does not match the managed XML; manual review is required"
        )
    recovered = dict(state)
    recovered.pop("pending", None)
    atomic_write_state(config, recovered)
    return recovered


def history_digest(config: RuntimeConfig) -> str:
    payload = "\n".join(accepted_hashes(config)).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _default_engine_runner(
    command: list[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, **kwargs)


def portfolio_performance_is_running() -> bool:
    """Return true when the desktop app could overwrite the managed XML."""
    try:
        if sys.platform == "darwin":
            for command in (
                ["pgrep", "-x", "PortfolioPerformance"],
                ["pgrep", "-f", "PortfolioPerformance.app/Contents/MacOS"],
            ):
                result = subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if result.returncode == 0:
                    return True
                if result.returncode != 1:
                    raise ImportDeferred(
                        "Cannot determine whether Portfolio Performance is open: "
                        f"{' '.join(command)} exited {result.returncode}"
                    )
            return False
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq PortfolioPerformance.exe"],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                raise ImportDeferred(
                    "Cannot determine whether Portfolio Performance is open: "
                    f"tasklist exited {result.returncode}"
                )
            return "portfolioperformance.exe" in result.stdout.lower()
    except OSError as error:
        raise ImportDeferred(
            f"Cannot determine whether Portfolio Performance is open: {error}"
        ) from error
    return False


def _run_engine(
    config: RuntimeConfig,
    arguments: list[str],
    runner: EngineRunner,
) -> str:
    if not config.engine_launcher.is_file():
        raise ImportWorkflowError(
            f"Portfolio Performance engine launcher not found: {config.engine_launcher}"
        )
    command = [sys.executable, str(config.engine_launcher), *arguments]
    result = runner(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ImportWorkflowError(
            f"Official Portfolio Performance engine failed ({result.returncode}): {detail}"
        )
    summary = result.stdout.strip().splitlines()
    if not summary or not summary[-1].startswith("OK|"):
        raise ImportWorkflowError(
            f"Unexpected Portfolio Performance engine response: {result.stdout.strip()}"
        )
    return summary[-1]


def create_xml_backup(config: RuntimeConfig) -> Path:
    if config.xml_file.is_symlink() or not config.xml_file.is_file():
        raise ImportWorkflowError(
            f"Managed XML is not a regular file: {config.xml_file}"
        )
    original_hash = sha256_file(config.xml_file)
    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
    filename = (
        f"{config.xml_file.stem}.{timestamp}.{original_hash[:12]}"
        f"{config.xml_file.suffix}"
    )
    backup = unique_destination(config.backup_dir, filename)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=config.backup_dir,
            prefix=f".{backup.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            with config.xml_file.open("rb") as source:
                shutil.copyfileobj(source, stream)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copystat(config.xml_file, temporary)
        if sha256_file(temporary) != original_hash:
            raise ImportWorkflowError("XML backup hash verification failed")
        os.replace(temporary, backup)
        temporary = None
        _fsync_directory(config.backup_dir)
        if config.state_file.is_file():
            state_backup = Path(str(backup) + ".state.json")
            state_temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=config.backup_dir,
                    prefix=f".{state_backup.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    state_temporary = Path(stream.name)
                    with config.state_file.open("rb") as source:
                        shutil.copyfileobj(source, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                if sha256_file(state_temporary) != sha256_file(config.state_file):
                    raise ImportWorkflowError("State backup hash verification failed")
                os.replace(state_temporary, state_backup)
                state_temporary = None
                _fsync_directory(config.backup_dir)
            except Exception:
                backup.unlink(missing_ok=True)
                state_backup.unlink(missing_ok=True)
                _fsync_directory(config.backup_dir)
                raise
            finally:
                if state_temporary is not None:
                    state_temporary.unlink(missing_ok=True)
        return backup
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _restore_previous_xml(
    config: RuntimeConfig,
    backup: Path | None,
) -> None:
    if backup is None:
        config.xml_file.unlink(missing_ok=True)
        _fsync_directory(config.xml_file.parent)
        return
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=config.xml_file.parent,
            prefix=f".{config.xml_file.name}.rollback.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            with backup.open("rb") as source:
                shutil.copyfileobj(source, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config.xml_file)
        temporary = None
        _fsync_directory(config.xml_file.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def import_csv_into_xml(
    config: RuntimeConfig,
    csv_path: Path,
    *,
    expected_rows: int | None = None,
    runner: EngineRunner = _default_engine_runner,
    before_publish: Callable[[PublishResult], None] | None = None,
) -> PublishResult:
    if config.xml_file.is_symlink():
        raise ImportWorkflowError(
            f"Managed Portfolio XML must not be a symlink: {config.xml_file}"
        )
    if config.xml_file.exists() and not config.xml_file.is_file():
        raise ImportWorkflowError(
            f"Managed Portfolio XML is not a regular file: {config.xml_file}"
        )

    temporary: Path | None = None
    backup: Path | None = None
    source_hash = sha256_file(config.xml_file) if config.xml_file.is_file() else ""
    published = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=config.xml_file.parent,
            prefix=f".{config.xml_file.name}.build.",
            suffix=".xml",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
        import_arguments = ["--csv", str(csv_path)]
        if config.xml_file.is_file():
            import_arguments.extend(["--existing", str(config.xml_file)])
        import_arguments.extend(["--output", str(temporary)])
        if config.base_currency:
            import_arguments.extend(["--base-currency", config.base_currency])
        engine_summary = _run_engine(config, import_arguments, runner)
        summary = parse_engine_summary(engine_summary)
        for required in ("added", "skipped", "logical"):
            if required not in summary:
                raise ImportWorkflowError(
                    f"Official engine response has no {required}: {engine_summary}"
                )
        if expected_rows is not None and summary["added"] + summary["skipped"] != expected_rows:
            raise ImportWorkflowError(
                "Official engine accounted for "
                f"{summary['added'] + summary['skipped']} CSV rows; expected {expected_rows}"
            )
        if temporary.is_symlink() or not temporary.is_file():
            raise ImportWorkflowError("Engine did not create a regular XML file")
        ElementTree.parse(temporary)
        semantic = parse_engine_summary(
            _run_engine(
                config,
                ["--verify-import", str(csv_path), str(temporary)],
                runner,
            )
        )
        if expected_rows is not None and semantic.get("semanticRows") != expected_rows:
            raise ImportWorkflowError(
                "Semantic verification row count differs from the normalized CSV"
            )
        verification = parse_engine_summary(
            _run_engine(config, ["--verify", str(temporary)], runner)
        )
        if verification.get("logical") != summary["logical"]:
            raise ImportWorkflowError(
                "Temporary XML transaction count differs from the import result"
            )
        _fsync_file(temporary)
        new_hash = sha256_file(temporary)

        if portfolio_performance_is_running():
            raise ImportDeferred(
                "Portfolio Performance is open; import will retry after the app is closed"
            )
        current_hash = sha256_file(config.xml_file) if config.xml_file.is_file() else ""
        if current_hash != source_hash:
            raise ImportWorkflowError(
                "Managed Portfolio XML changed during import; retry with Portfolio Performance closed"
            )
        if config.xml_file.is_file():
            backup = create_xml_backup(config)
        planned_result = PublishResult(
            xml_path=config.xml_file,
            xml_sha256=new_hash,
            backup_path=backup,
            engine_summary=engine_summary,
            added_transactions=summary["added"],
            skipped_transactions=summary["skipped"],
            logical_transactions=summary["logical"],
        )
        if before_publish is not None:
            before_publish(planned_result)
        os.replace(temporary, config.xml_file)
        temporary = None
        published = True
        _fsync_directory(config.xml_file.parent)
        official_summary = _run_engine(
            config, ["--verify", str(config.xml_file)], runner
        )
        if sha256_file(config.xml_file) != new_hash:
            raise ImportWorkflowError("Published XML hash differs from verified XML")

        return planned_result
    except Exception as error:
        if published:
            try:
                _restore_previous_xml(config, backup)
            except Exception as rollback_error:
                raise RollbackFailed(
                    f"{error}; XML rollback failed: {rollback_error}"
                ) from error
        if isinstance(error, ImportWorkflowError):
            raise
        raise ImportWorkflowError(str(error)) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parse_engine_summary(summary: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for item in summary.split("|")[1:]:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        try:
            values[key] = int(value)
        except ValueError as error:
            raise ImportWorkflowError(
                f"Invalid numeric value in engine response: {item}"
            ) from error
    return values


def write_engine_csv(
    records: Iterable[CanonicalRecord],
    directory: Path,
    file_sha256: str,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        directory.chmod(0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=directory,
            prefix=".normalized-transactions.",
            suffix=".csv",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(ENGINE_CSV_HEADERS)
            for record in records:
                writer.writerow(record.engine_row(file_sha256))
            stream.flush()
            os.fsync(stream.fileno())
        result = temporary
        temporary = None
        return result
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def cleanup_engine_temp(config: RuntimeConfig) -> int:
    """Remove only crash-orphan normalized rows from the private engine directory."""
    config.engine_temp_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        config.engine_temp_dir.chmod(0o700)
    removed = 0
    for candidate in config.engine_temp_dir.glob(".normalized-transactions.*.csv"):
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink()
            removed += 1
    if removed:
        _fsync_directory(config.engine_temp_dir)
    return removed


def cleanup_claim_temp(config: RuntimeConfig) -> int:
    """Remove only crash-orphan claim snapshots while holding the watcher lock."""
    removed = 0
    for candidate in config.processing_dir.glob(".*.claim.tmp"):
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink()
            removed += 1
    if removed:
        _fsync_directory(config.processing_dir)
    return removed


def _identity_audit(records: Iterable[CanonicalRecord]) -> dict[str, Any]:
    material = tuple(records)
    return {
        "source_systems": sorted({row.source_system for row in material if row.source_system}),
        "source_document_ids": sorted(
            {row.source_document_id for row in material if row.source_document_id}
        ),
        "external_transaction_ids": sorted(
            {row.external_transaction_id for row in material if row.external_transaction_id}
        ),
        "identity_count": len(material),
    }


def _audit_outcome(
    config: RuntimeConfig,
    event: str,
    *,
    csv_path: Path | None,
    csv_sha256: str,
    outcome: str,
    backup_path: Path | None = None,
    error: str = "",
    **details: Any,
) -> str:
    """Record an outcome without allowing audit I/O to invert durable state."""
    failures = []
    try:
        write_log(
            config,
            event,
            csv_path=csv_path,
            csv_sha256=csv_sha256,
            outcome=outcome,
            backup_path=backup_path,
            error=error,
            **details,
        )
    except Exception as audit_error:
        failures.append(f"log: {audit_error}")
    try:
        write_report(
            config,
            event,
            csv_path=csv_path,
            csv_sha256=csv_sha256,
            outcome=outcome,
            error=error,
            backup_path=str(backup_path) if backup_path else "",
            **details,
        )
    except Exception as audit_error:
        failures.append(f"report: {audit_error}")
    return "; ".join(failures)


def process_file(
    config: RuntimeConfig,
    csv_path: Path,
    *,
    runner: EngineRunner = _default_engine_runner,
) -> ProcessResult:
    original_csv_path = csv_path
    claimed_by_call = False
    csv_hash = ""
    history_path: Path | None = None
    published_result: PublishResult | None = None
    normalized_csv: Path | None = None
    pending_state: dict[str, Any] | None = None
    ledger_committed = False
    audit_details: dict[str, Any] = {}
    try:
        if csv_path.is_symlink():
            raise ImportWorkflowError(f"CSV symlinks are not allowed: {csv_path}")
        if portfolio_performance_is_running():
            raise ImportDeferred(
                "Portfolio Performance is open; import is deferred until the app is closed"
            )
        observed_size = csv_path.stat().st_size
        if observed_size > config.csv_max_file_bytes:
            history_path = unique_destination(config.rejected_dir, csv_path.name)
            _move_csv_atomic(csv_path, history_path)
            message = (
                f"CSV exceeds the configured byte limit ({config.csv_max_file_bytes})"
            )
            audit_error = _audit_outcome(
                config,
                "oversized_csv_rejected_without_hashing",
                csv_path=history_path,
                csv_sha256="",
                outcome="rejected",
                error=message,
                observed_file_bytes=observed_size,
            )
            return ProcessResult(
                outcome="rejected",
                csv_sha256="",
                destination=history_path,
                backup_path=None,
                xml_path=config.xml_file if config.xml_file.is_file() else None,
                error="; ".join(filter(None, (message, audit_error))),
            )
        if csv_path.parent != config.processing_dir:
            if not wait_until_stable(csv_path, config):
                raise ImportDeferred(
                    "CSV is still syncing; it remains unchanged in the inbox"
                )
            try:
                csv_path = claim_csv(
                    csv_path,
                    config.processing_dir,
                    max_file_bytes=config.csv_max_file_bytes,
                )
            except OSError as error:
                raise ImportDeferred(
                    "CSV is temporarily locked or not hydrated; it remains queued in the inbox"
                ) from error
            claimed_by_call = True
            try:
                write_log(
                    config,
                    "csv_claimed_for_processing",
                    csv_path=csv_path,
                    outcome="processing",
                    inbox_path=str(original_csv_path),
                )
            except Exception:
                pass
        if not wait_until_stable(csv_path, config):
            raise ImportDeferred(
                "Claimed CSV is still changing; it remains recoverably in processing"
            )
        try:
            csv_hash = sha256_file(csv_path)
        except OSError as error:
            raise ImportDeferred(
                "Claimed CSV is temporarily unreadable; it remains queued for retry"
            ) from error
        state = reconcile_pending_state(
            config,
            load_state(config),
            current_file_sha256=csv_hash,
        )
        validate_committed_xml_state(config, state)
        accepted = set(state.get("processed_sha256", []))
        if csv_hash in accepted:
            history_path = route_unchanged_csv(
                csv_path, config.processed_dir, csv_hash
            )
            audit_error = _audit_outcome(
                config,
                "duplicate_csv_accepted_without_rebuild",
                csv_path=history_path,
                csv_sha256=csv_hash,
                outcome="duplicate_skipped",
            )
            return ProcessResult(
                outcome="duplicate_skipped",
                csv_sha256=csv_hash,
                destination=history_path,
                backup_path=None,
                xml_path=config.xml_file if config.xml_file.is_file() else None,
                error=audit_error,
            )

        try:
            records = read_canonical_records(csv_path, config)
            hash_after_parse = sha256_file(csv_path)
        except OSError as error:
            raise ImportDeferred(
                "Claimed CSV became unavailable during sync; it remains queued for retry"
            ) from error
        if hash_after_parse != csv_hash:
            raise ImportDeferred(
                "Claimed CSV changed while it was being parsed; it remains in processing"
            )
        row_count = len(records)
        audit_details = _identity_audit(records)
        decision = assess_identities(records, state, csv_hash)
        if decision.outcome == "needs_review":
            history_path = route_unchanged_csv(
                csv_path, config.needs_review_dir, csv_hash
            )
            audit_error = _audit_outcome(
                config,
                "csv_needs_review",
                csv_path=history_path,
                csv_sha256=csv_hash,
                outcome="needs_review",
                error=decision.reason,
                rows=row_count,
                **audit_details,
            )
            return ProcessResult(
                outcome="needs_review",
                csv_sha256=csv_hash,
                destination=history_path,
                backup_path=None,
                xml_path=config.xml_file if config.xml_file.is_file() else None,
                error="; ".join(filter(None, (decision.reason, audit_error))),
            )
        if decision.outcome == "reject":
            raise CSVValidationError(decision.reason)
        if decision.outcome == "skip":
            current_xml_hash = sha256_file(config.xml_file) if config.xml_file.is_file() else ""
            committed = commit_identity_ledger(
                state,
                records,
                file_sha256=csv_hash,
                xml_sha256=current_xml_hash,
            )
            history_path = route_unchanged_csv(
                csv_path, config.processed_dir, csv_hash
            )
            if sha256_file(history_path) != csv_hash:
                raise ImportWorkflowError(
                    "Processed CSV changed before its identity-only ledger commit"
                )
            try:
                atomic_write_state(config, committed)
            except Exception:
                try:
                    observed_state = load_state(config)
                except Exception:
                    observed_state = {}
                if _state_contains_commit(observed_state, committed):
                    ledger_committed = True
                    audit_details["state_commit_warning"] = (
                        "Identity-only ledger rename succeeded; its durability barrier "
                        "reported an error"
                    )
                else:
                    raise
            else:
                ledger_committed = True
            audit_error = _audit_outcome(
                config,
                "external_id_rows_skipped",
                csv_path=history_path,
                csv_sha256=csv_hash,
                outcome="duplicate_skipped",
                rows=row_count,
                skipped_transactions=len(decision.skipped_records),
                **audit_details,
            )
            return ProcessResult(
                outcome="duplicate_skipped",
                csv_sha256=csv_hash,
                destination=history_path,
                backup_path=None,
                xml_path=config.xml_file if config.xml_file.is_file() else None,
                error=audit_error,
            )

        xml_hash_before = sha256_file(config.xml_file) if config.xml_file.is_file() else ""
        pending_state = dict(state)
        pending_state["pending"] = {
            "file_sha256": csv_hash,
            "xml_sha256_before": xml_hash_before,
            "identity_keys": [record.identity_key(csv_hash) for record in records],
            "payload_sha256": [record.payload_sha256() for record in records],
            "claimed_csv": str(csv_path),
            "stage": "processing",
        }
        atomic_write_state(config, pending_state)
        pending_state["generation"] = int(pending_state.get("generation", 0)) + 1
        normalized_csv = write_engine_csv(
            decision.new_records,
            config.engine_temp_dir,
            csv_hash,
        )

        staged_committed: dict[str, Any] | None = None

        def stage_publication(plan: PublishResult) -> None:
            nonlocal staged_committed
            committed = commit_identity_ledger(
                pending_state,
                records,
                file_sha256=csv_hash,
                xml_sha256=plan.xml_sha256,
            )
            committed.update(
                {
                    "xml_file": str(plan.xml_path),
                    "xml_sha256": plan.xml_sha256,
                    "engine_summary": plan.engine_summary,
                    "logical_transactions": plan.logical_transactions,
                    "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
            )
            finalizing_state = dict(pending_state)
            finalizing_pending = dict(finalizing_state["pending"])
            finalizing_pending.update(
                {
                    "stage": "publishing",
                    "xml_sha256_after": plan.xml_sha256,
                    "backup_path": str(plan.backup_path) if plan.backup_path else "",
                    "commit_state": committed,
                }
            )
            finalizing_state["pending"] = finalizing_pending
            atomic_write_state(config, finalizing_state)
            staged_committed = committed

        result = import_csv_into_xml(
            config,
            normalized_csv,
            expected_rows=len(decision.new_records),
            runner=runner,
            before_publish=stage_publication,
        )
        published_result = result
        if sha256_file(csv_path) != csv_hash:
            raise ImportWorkflowError(
                "Claimed CSV changed during import; publication must be rolled back"
            )
        if staged_committed is None:
            raise ImportWorkflowError("Publication journal was not staged")
        history_path = route_unchanged_csv(
            csv_path, config.processed_dir, csv_hash
        )
        if sha256_file(history_path) != csv_hash:
            raise ImportWorkflowError(
                "Processed CSV changed before the final ledger commit"
            )
        try:
            atomic_write_state(config, staged_committed)
        except Exception as error:
            try:
                observed_state = load_state(config)
            except Exception:
                observed_state = {}
            observed_xml = (
                sha256_file(config.xml_file) if config.xml_file.is_file() else ""
            )
            if (
                observed_xml == result.xml_sha256
                and _state_contains_commit(observed_state, staged_committed)
            ):
                ledger_committed = True
                audit_details["state_commit_warning"] = (
                    "Final ledger rename succeeded; its durability barrier reported an error"
                )
            else:
                raise FinalStateCommitDeferred(
                    "Published XML and routed CSV are retained with their finalizing journal; "
                    "restart the watcher to complete the ledger commit"
                ) from error
        else:
            ledger_committed = True
        audit_error = _audit_outcome(
            config,
            "csv_applied_to_managed_portfolio",
            csv_path=history_path,
            csv_sha256=csv_hash,
            outcome="processed",
            backup_path=result.backup_path,
            rows=row_count,
            xml_sha256=result.xml_sha256,
            engine_summary=result.engine_summary,
            added_transactions=result.added_transactions,
            skipped_transactions=result.skipped_transactions,
            logical_transactions=result.logical_transactions,
            **audit_details,
        )
        return ProcessResult(
            outcome="processed",
            csv_sha256=csv_hash,
            destination=history_path,
            backup_path=result.backup_path,
            xml_path=result.xml_path,
            error=audit_error,
        )
    except FinalStateCommitDeferred as error:
        audit_error = _audit_outcome(
            config,
            "final_state_commit_deferred",
            csv_path=history_path or csv_path,
            csv_sha256=csv_hash,
            outcome="deferred",
            error=str(error),
            **audit_details,
        )
        return ProcessResult(
            outcome="deferred",
            csv_sha256=csv_hash,
            destination=history_path or csv_path,
            backup_path=published_result.backup_path if published_result else None,
            xml_path=config.xml_file if config.xml_file.is_file() else None,
            error="; ".join(filter(None, (str(error), audit_error))),
        )
    except ImportNeedsReview as error:
        if csv_path.exists() and not csv_path.is_symlink():
            if not csv_hash:
                csv_hash = sha256_file(csv_path)
            history_path = route_unchanged_csv(
                csv_path, config.needs_review_dir, csv_hash
            )
        audit_error = _audit_outcome(
            config,
            "csv_needs_review",
            csv_path=history_path or csv_path,
            csv_sha256=csv_hash,
            outcome="needs_review",
            error=str(error),
            **audit_details,
        )
        return ProcessResult(
            outcome="needs_review",
            csv_sha256=csv_hash,
            destination=history_path,
            backup_path=None,
            xml_path=config.xml_file if config.xml_file.is_file() else None,
            error="; ".join(filter(None, (str(error), audit_error))),
        )
    except ImportDeferred as error:
        if pending_state is not None and not ledger_committed:
            try:
                recovered = dict(pending_state)
                recovered.pop("pending", None)
                atomic_write_state(config, recovered)
            except Exception:
                pass
        if (
            claimed_by_call
            and "Portfolio Performance" in str(error)
            and csv_path.exists()
            and not original_csv_path.exists()
        ):
            try:
                _move_csv_atomic(csv_path, original_csv_path)
                csv_path = original_csv_path
            except Exception:
                pass
        audit_error = _audit_outcome(
            config,
            "import_deferred",
            csv_path=csv_path,
            csv_sha256=csv_hash,
            outcome="deferred",
            error=str(error),
            **audit_details,
        )
        return ProcessResult(
            outcome="deferred",
            csv_sha256=csv_hash,
            destination=csv_path,
            backup_path=None,
            xml_path=config.xml_file if config.xml_file.is_file() else None,
            error="; ".join(filter(None, (str(error), audit_error))),
        )
    except Exception as error:
        error_text = f"{type(error).__name__}: {error}"
        rollback_failed = isinstance(error, RollbackFailed)
        move_uncertain = isinstance(error, CsvMoveUncertain)
        state_cleanup_failed = False
        if published_result is not None and not ledger_committed:
            try:
                _restore_previous_xml(config, published_result.backup_path)
            except Exception as rollback_error:
                error_text += f"; XML rollback failed: {rollback_error}"
                rollback_failed = True
        source = history_path if history_path is not None else csv_path
        if move_uncertain and error.destination.exists():
            source = error.destination
        if rollback_failed:
            recovery_path: Path | None = source if source.exists() else None
            if recovery_path is not None and recovery_path.parent != config.processing_dir:
                try:
                    processing_path = unique_destination(
                        config.processing_dir,
                        recovery_path.name,
                    )
                    _move_csv_atomic(recovery_path, processing_path)
                    recovery_path = processing_path
                except Exception as routing_error:
                    error_text += f"; recovery routing failed: {routing_error}"
            audit_error = _audit_outcome(
                config,
                "rollback_needs_review",
                csv_path=recovery_path or source,
                csv_sha256=csv_hash,
                outcome="needs_review",
                error=error_text,
                **audit_details,
            )
            return ProcessResult(
                outcome="needs_review",
                csv_sha256=csv_hash,
                destination=recovery_path,
                backup_path=None,
                xml_path=config.xml_file if config.xml_file.is_file() else None,
                error="; ".join(filter(None, (error_text, audit_error))),
            )
        if move_uncertain:
            audit_error = _audit_outcome(
                config,
                "csv_move_deferred",
                csv_path=source,
                csv_sha256=csv_hash,
                outcome="deferred",
                error=error_text,
                intended_source=str(error.source),
                observed_destination=str(error.destination),
                **audit_details,
            )
            return ProcessResult(
                outcome="deferred",
                csv_sha256=csv_hash,
                destination=source if source.exists() else None,
                backup_path=None,
                xml_path=config.xml_file if config.xml_file.is_file() else None,
                error="; ".join(filter(None, (error_text, audit_error))),
            )
        if pending_state is not None and not ledger_committed:
            try:
                recovered = dict(pending_state)
                recovered.pop("pending", None)
                atomic_write_state(config, recovered)
            except Exception as state_error:
                error_text += f"; pending ledger cleanup failed: {state_error}"
                state_cleanup_failed = True
        if state_cleanup_failed:
            recovery_path: Path | None = source if source.exists() else None
            if recovery_path is not None and recovery_path.parent != config.processing_dir:
                try:
                    processing_path = unique_destination(
                        config.processing_dir,
                        recovery_path.name,
                    )
                    _move_csv_atomic(recovery_path, processing_path)
                    recovery_path = processing_path
                except Exception as routing_error:
                    error_text += f"; recovery routing failed: {routing_error}"
            audit_error = _audit_outcome(
                config,
                "state_cleanup_deferred",
                csv_path=recovery_path or source,
                csv_sha256=csv_hash,
                outcome="deferred",
                error=error_text,
                **audit_details,
            )
            return ProcessResult(
                outcome="deferred",
                csv_sha256=csv_hash,
                destination=recovery_path,
                backup_path=None,
                xml_path=config.xml_file if config.xml_file.is_file() else None,
                error="; ".join(filter(None, (error_text, audit_error))),
            )
        destination: Path | None = None
        if source.exists() and not source.is_symlink():
            try:
                routed_hash = sha256_file(source)
                if csv_hash and csv_hash != routed_hash:
                    audit_details = {
                        **audit_details,
                        "initial_csv_sha256": csv_hash,
                    }
                csv_hash = routed_hash
                destination = route_unchanged_csv(
                    source, config.rejected_dir, routed_hash
                )
            except Exception as routing_error:
                error_text += f"; routing failed: {routing_error}"
        audit_error = _audit_outcome(
            config,
            "csv_rejected",
            csv_path=destination or source,
            csv_sha256=csv_hash,
            outcome="rejected",
            error=error_text,
            destination=str(destination) if destination else "",
            **audit_details,
        )
        return ProcessResult(
            outcome="rejected",
            csv_sha256=csv_hash,
            destination=destination,
            backup_path=None,
            xml_path=config.xml_file if config.xml_file.is_file() else None,
            error="; ".join(filter(None, (error_text, audit_error))),
        )
    finally:
        if normalized_csv is not None:
            try:
                normalized_csv.unlink(missing_ok=True)
                _fsync_directory(config.engine_temp_dir)
            except OSError:
                # A watcher restart removes only these private normalized files
                # while holding the single-instance lock.
                pass


def recover_finalizing_import(config: RuntimeConfig) -> bool:
    """Finish the durable route/ledger handoff after a process or power loss."""
    state = load_state(config)
    pending = state.get("pending")
    if not isinstance(pending, dict) or pending.get("stage") not in {
        "publishing",
        "published",
    }:
        return False
    file_sha256 = pending.get("file_sha256")
    xml_before = pending.get("xml_sha256_before")
    xml_after = pending.get("xml_sha256_after")
    commit_state = pending.get("commit_state")
    if not (
        isinstance(file_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", file_sha256)
        and isinstance(xml_before, str)
        and isinstance(xml_after, str)
        and re.fullmatch(r"[0-9a-f]{64}", xml_after)
        and isinstance(commit_state, dict)
    ):
        raise ImportNeedsReview("Published pending journal is malformed")
    current_xml = sha256_file(config.xml_file) if config.xml_file.is_file() else ""
    if current_xml == xml_before:
        processing_matches = [
            path
            for path in discover_csv_files(config.processing_dir)
            if sha256_file(path) == file_sha256
        ]
        processed_matches = [
            path
            for path in discover_csv_files(config.processed_dir)
            if sha256_file(path) == file_sha256
        ]
        if len(processing_matches) + len(processed_matches) > 1:
            raise ImportNeedsReview(
                "Rolled-back pending import has multiple matching CSV files"
            )
        if processed_matches:
            retry_path = unique_destination(
                config.processing_dir,
                processed_matches[0].name,
            )
            _move_csv_atomic(processed_matches[0], retry_path)
        recovered = dict(state)
        recovered.pop("pending", None)
        atomic_write_state(config, recovered)
        return False
    if current_xml != xml_after:
        raise ImportNeedsReview(
            "Published pending journal matches neither the prior nor verified XML"
        )

    processing_matches = [
        path
        for path in discover_csv_files(config.processing_dir)
        if sha256_file(path) == file_sha256
    ]
    processed_matches = [
        path
        for path in discover_csv_files(config.processed_dir)
        if sha256_file(path) == file_sha256
    ]
    if len(processing_matches) + len(processed_matches) != 1:
        raise ImportNeedsReview(
            "Published import cannot identify exactly one claimed/processed CSV"
        )
    if processing_matches:
        final_path = route_unchanged_csv(
            processing_matches[0],
            config.processed_dir,
            file_sha256,
        )
    else:
        final_path = processed_matches[0]
    atomic_write_state(config, commit_state)
    _audit_outcome(
        config,
        "published_import_recovered",
        csv_path=final_path,
        csv_sha256=file_sha256,
        outcome="processed",
        backup_path=Path(pending["backup_path"]) if pending.get("backup_path") else None,
        recovered_after_restart=True,
    )
    return True


def dry_run_plan(config: RuntimeConfig, csv_path: Path) -> bool:
    try:
        if not wait_until_stable(csv_path, config):
            raise CSVValidationError("CSV did not reach the stability threshold")
        rows = validate_csv(csv_path, config)
        csv_hash = sha256_file(csv_path)
        duplicate = csv_hash in set(accepted_hashes(config))
        xml_exists = config.xml_file.is_file() and not config.xml_file.is_symlink()
        actions = (
            f"validate {rows} CSV rows and the fixed schema",
            f"compare SHA-256 with successfully imported CSV in {config.processed_dir}",
            (
                "skip import because identical CSV content is already accepted"
                if duplicate else (
                    f"incrementally add this CSV to {config.xml_file}"
                    if xml_exists
                    else f"create the first managed Portfolio XML at {config.xml_file}"
                )
            ),
            f"use official Portfolio Performance model via {config.engine_launcher}",
            "save to a temporary XML and reopen it with ClientFactory.load",
            (
                f"backup the managed XML in {config.backup_dir}"
                if xml_exists else "no backup is needed because the managed XML does not exist yet"
            ),
            f"atomically publish the managed XML only after semantic and transaction-count verification",
            f"move the unchanged CSV to {config.processed_dir}",
        )
        for step, action in enumerate(actions, start=1):
            write_log(
                config,
                "dry_run_plan",
                csv_path=csv_path,
                csv_sha256=csv_hash,
                outcome="planned",
                step=step,
                action=action,
            )
        return True
    except Exception as error:
        write_log(
            config,
            "dry_run_validation_failed",
            csv_path=csv_path,
            outcome="error",
            error=f"{type(error).__name__}: {error}",
        )
        return False


def _mode_from_args(
    config: RuntimeConfig, dry_run_flag: bool, execute_flag: bool
) -> bool:
    if dry_run_flag:
        return True
    if execute_flag:
        return False
    return config.dry_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and incrementally update one managed Portfolio Performance XML."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("config.json"),
    )
    parser.add_argument("--once", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    return parser


def run(
    argv: Iterable[str] | None = None,
    *,
    runner: EngineRunner = _default_engine_runner,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        ensure_directories(config)
    except (ConfigurationError, OSError) as error:
        sys.stderr.write(f"Configuration error: {error}\n")
        return 2

    dry_run = _mode_from_args(config, args.dry_run, args.execute)
    stopped = False

    def request_stop(_signal: int, _frame: Any) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    exit_code = 0
    try:
        with SingleInstanceLock(config.lock_file):
            mode = "dry_run" if dry_run else "execute"
            cleanup_claim_temp(config)
            cleanup_engine_temp(config)
            recovery_pending = False
            if not dry_run:
                try:
                    recovery_pending = portfolio_performance_is_running()
                except ImportDeferred as error:
                    recovery_pending = True
                    _audit_outcome(
                        config,
                        "pending_recovery_deferred",
                        csv_path=None,
                        csv_sha256="",
                        outcome="deferred",
                        error=str(error),
                    )
                else:
                    if recovery_pending:
                        _audit_outcome(
                            config,
                            "pending_recovery_deferred",
                            csv_path=None,
                            csv_sha256="",
                            outcome="deferred",
                            error=(
                                "Portfolio Performance is open; pending recovery and "
                                "CSV enumeration are deferred"
                            ),
                        )
                if not recovery_pending:
                    try:
                        recover_finalizing_import(config)
                    except ImportWorkflowError as error:
                        _audit_outcome(
                            config,
                            "pending_finalization_needs_review",
                            csv_path=None,
                            csv_sha256="",
                            outcome="needs_review",
                            error=str(error),
                        )
                        return 1
            if not recovery_pending:
                try:
                    validate_committed_xml_state(config, load_state(config))
                except ImportWorkflowError as error:
                    _audit_outcome(
                        config,
                        "managed_xml_state_mismatch",
                        csv_path=None,
                        csv_sha256="",
                        outcome="needs_review",
                        error=str(error),
                    )
                    return 1
            write_log(config, "watcher_started", outcome=mode, once=args.once)
            try:
                while not stopped:
                    if recovery_pending:
                        try:
                            recovery_pending = portfolio_performance_is_running()
                        except ImportDeferred as error:
                            recovery_pending = True
                            _audit_outcome(
                                config,
                                "pending_recovery_deferred",
                                csv_path=None,
                                csv_sha256="",
                                outcome="deferred",
                                error=str(error),
                            )
                        if recovery_pending:
                            if args.once:
                                break
                            time.sleep(config.poll_interval_seconds)
                            continue
                        try:
                            recover_finalizing_import(config)
                            validate_committed_xml_state(config, load_state(config))
                        except ImportWorkflowError as error:
                            _audit_outcome(
                                config,
                                "pending_finalization_needs_review",
                                csv_path=None,
                                csv_sha256="",
                                outcome="needs_review",
                                error=str(error),
                            )
                            return 1
                    claimed = discover_csv_files(config.processing_dir)
                    inbox = discover_csv_files(config.incoming_dir)
                    for csv_path in [*claimed, *inbox]:
                        if dry_run:
                            if not dry_run_plan(config, csv_path):
                                exit_code = 1
                        else:
                            result = process_file(
                                config, csv_path, runner=runner
                            )
                            if result.outcome == "rejected":
                                exit_code = 1
                    if args.once:
                        break
                    time.sleep(config.poll_interval_seconds)
            finally:
                write_log(
                    config,
                    "watcher_stopped",
                    outcome=mode,
                    stop_requested=stopped,
                    exit_code=exit_code,
                )
    except WatcherAlreadyRunningError as error:
        sys.stderr.write(f"{error}\n")
        return 2
    return exit_code


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
