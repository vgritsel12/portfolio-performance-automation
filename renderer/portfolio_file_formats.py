"""Bounded, privacy-safe bridge to official Portfolio Performance formats."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Iterator
import zipfile


MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_NORMALIZED_BYTES = 256 * 1024 * 1024
MAX_EXPANSION_RATIO = 100
DECODE_TIMEOUT_SECONDS = 120
ENCRYPTED_SIGNATURE = b"PORTFOLIO"
ZIP_SIGNATURE = b"PK\x03\x04"
STATUS_PATTERN = re.compile(
    r"^STATUS=(OK|FAILED)\|CODE=([A-Z0-9_]+)\|FORMAT=([A-Z0-9_]+)\|BODY=([A-Z0-9_]+)"
    r"(?:\|SOURCE_SHA256=([0-9a-f]{64}))?"
    r"(?:\|NORMALIZED_SHA256=([0-9a-f]{64}))?"
    r"(?:\|NORMALIZED_BYTES=(\d+))?$"
)


class PortfolioFileKind(str, Enum):
    XML = "XML"
    XML_ID = "XML_ID"
    XML_ZIP = "XML_ZIP"
    BINARY = "BINARY"
    ENCRYPTED = "ENCRYPTED"
    EMPTY = "EMPTY"
    UNSUPPORTED = "UNSUPPORTED"
    TRUNCATED = "TRUNCATED"
    UNSUPPORTED_AES = "UNSUPPORTED_AES"
    DAMAGED_ZIP = "DAMAGED_ZIP"


class PortfolioFormatError(RuntimeError):
    """A stable safe input/decoder failure with no private detail."""

    def __init__(self, code: str):
        if not re.fullmatch(r"E_[A-Z0-9_]+", code):
            code = "E_DECODE_FAILED"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PortfolioInspection:
    kind: PortfolioFileKind
    encrypted: bool
    body: str
    source_bytes: int
    source_sha256: str


@dataclass(frozen=True)
class NormalizedPortfolio:
    path: Path
    kind: PortfolioFileKind
    body: str
    source_bytes: int
    normalized_bytes: int
    source_sha256: str
    normalized_sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _zip_kind(path: Path) -> PortfolioFileKind:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = [item for item in archive.infolist() if not item.is_dir()]
            if not entries:
                return PortfolioFileKind.DAMAGED_ZIP
            return (
                PortfolioFileKind.BINARY
                if entries[0].filename.endswith(".portfolio")
                else PortfolioFileKind.XML_ZIP
            )
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return PortfolioFileKind.DAMAGED_ZIP


def inspect_portfolio_file(source: Path) -> PortfolioInspection:
    path = source.expanduser()
    if path.is_symlink() or not path.is_file():
        raise PortfolioFormatError("E_INPUT_READ")
    try:
        size = path.stat().st_size
        if size > MAX_SOURCE_BYTES:
            raise PortfolioFormatError("E_SOURCE_TOO_LARGE")
        digest = _sha256(path)
        with path.open("rb") as stream:
            head = stream.read(512)
    except PortfolioFormatError:
        raise
    except OSError as error:
        raise PortfolioFormatError("E_INPUT_READ") from error

    if not head:
        kind = PortfolioFileKind.EMPTY
    elif head.startswith(ENCRYPTED_SIGNATURE):
        if size < 42 or len(head) <= len(ENCRYPTED_SIGNATURE):
            kind = PortfolioFileKind.TRUNCATED
        elif head[len(ENCRYPTED_SIGNATURE)] not in (0, 1):
            kind = PortfolioFileKind.UNSUPPORTED_AES
        else:
            kind = PortfolioFileKind.ENCRYPTED
    elif head.startswith(ZIP_SIGNATURE):
        kind = _zip_kind(path)
    else:
        text = head[3:] if head.startswith(b"\xef\xbb\xbf") else head
        text = text.lstrip()
        if text.startswith(b"<"):
            kind = PortfolioFileKind.XML_ID if b"<client id=" in text else PortfolioFileKind.XML
        else:
            kind = PortfolioFileKind.UNSUPPORTED
    body = (
        "BINARY"
        if kind == PortfolioFileKind.BINARY
        else "ENCRYPTED"
        if kind == PortfolioFileKind.ENCRYPTED
        else "XML"
    )
    return PortfolioInspection(kind, kind == PortfolioFileKind.ENCRYPTED, body, size, digest)


def _engine_command(engine_root: Path, java: Path, source: Path, output: Path) -> list[str]:
    try:
        manifest = json.loads((engine_root / "manifest.json").read_text(encoding="utf-8"))
        entry = manifest["entry_points"]["format_bridge"]
        classpath = manifest["classpath"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise PortfolioFormatError("E_FORMAT_BRIDGE_INTEGRITY") from error
    if not isinstance(entry, str) or not isinstance(classpath, list) or not classpath:
        raise PortfolioFormatError("E_FORMAT_BRIDGE_INTEGRITY")
    resolved: list[str] = []
    for relative in classpath:
        candidate = engine_root / str(relative)
        if not candidate.is_file():
            raise PortfolioFormatError("E_FORMAT_BRIDGE_INTEGRITY")
        resolved.append(str(candidate))
    return [
        str(java),
        "-cp",
        os.pathsep.join(resolved),
        entry,
        "normalize",
        str(source),
        str(output),
    ]


def _parse_status(stdout: bytes) -> tuple[str, str, str, str, str, int]:
    if len(stdout) > 8192:
        raise PortfolioFormatError("E_DECODE_PROTOCOL")
    lines = stdout.decode("ascii", errors="ignore").splitlines()
    match = next((STATUS_PATTERN.fullmatch(line) for line in reversed(lines) if line), None)
    if match is None:
        raise PortfolioFormatError("E_DECODE_PROTOCOL")
    status, code, _format, body, source_hash, normalized_hash, normalized_bytes = match.groups()
    if status != "OK":
        raise PortfolioFormatError(code)
    if source_hash is None or normalized_hash is None or normalized_bytes is None:
        raise PortfolioFormatError("E_DECODE_PROTOCOL")
    return code, _format, body, source_hash, normalized_hash, int(normalized_bytes)


@contextmanager
def normalized_portfolio_file(
    source: Path,
    *,
    engine_root: Path,
    java: Path,
    password: bytes | bytearray | None = None,
    timeout_seconds: int = DECODE_TIMEOUT_SECONDS,
) -> Iterator[NormalizedPortfolio]:
    """Yield private normalized XML and remove it unconditionally afterward."""

    if timeout_seconds != DECODE_TIMEOUT_SECONDS:
        raise PortfolioFormatError("E_DECODE_TIMEOUT_POLICY")
    inspection = inspect_portfolio_file(source)
    failures = {
        PortfolioFileKind.EMPTY: "E_EMPTY",
        PortfolioFileKind.UNSUPPORTED: "E_UNSUPPORTED_FORMAT",
        PortfolioFileKind.TRUNCATED: "E_ENCRYPTED_HEADER",
        PortfolioFileKind.UNSUPPORTED_AES: "E_AES_METHOD_UNSUPPORTED",
        PortfolioFileKind.DAMAGED_ZIP: "E_ZIP_CORRUPT",
    }
    if inspection.kind in failures:
        raise PortfolioFormatError(failures[inspection.kind])
    if inspection.encrypted and password is None:
        raise PortfolioFormatError("E_PASSWORD_MISSING")

    private_root = Path(tempfile.mkdtemp(prefix="pp-private-normalized-"))
    try:
        try:
            private_root.chmod(0o700)
        except OSError as error:
            raise PortfolioFormatError("E_PRIVATE_TEMP") from error
        staged_source = private_root / "source.portfolio"
        normalized = private_root / "normalized.xml"
        try:
            shutil.copyfile(source, staged_source)
            staged_source.chmod(0o600)
        except OSError as error:
            raise PortfolioFormatError("E_PRIVATE_TEMP") from error
        if (
            staged_source.stat().st_size != inspection.source_bytes
            or _sha256(staged_source) != inspection.source_sha256
            or _sha256(source) != inspection.source_sha256
        ):
            raise PortfolioFormatError("E_SOURCE_CHANGED")
        command = _engine_command(engine_root, java, staged_source, normalized)
        secret = bytearray(password or b"")
        secret_line = bytearray(secret)
        if inspection.encrypted:
            secret_line.append(10)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                stdout, _stderr = process.communicate(
                    input=secret_line if inspection.encrypted else b"",
                    timeout=DECODE_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.communicate()
                raise PortfolioFormatError("E_DECODE_TIMEOUT") from error
        except OSError as error:
            raise PortfolioFormatError("E_FORMAT_BRIDGE_INTEGRITY") from error
        finally:
            for index in range(len(secret)):
                secret[index] = 0
            for index in range(len(secret_line)):
                secret_line[index] = 0

        if process.returncode != 0:
            try:
                _parse_status(stdout)
            except PortfolioFormatError as error:
                if error.code != "E_DECODE_PROTOCOL":
                    raise
            raise PortfolioFormatError("E_DECODE_FAILED")
        _code, _format, body, source_hash, normalized_hash, normalized_bytes = _parse_status(stdout)
        if (
            source_hash != inspection.source_sha256
            or _sha256(staged_source) != inspection.source_sha256
            or _sha256(source) != inspection.source_sha256
        ):
            raise PortfolioFormatError("E_SOURCE_CHANGED")
        if not normalized.is_file():
            raise PortfolioFormatError("E_DECODE_PROTOCOL")
        actual_size = normalized.stat().st_size
        if normalized_bytes != actual_size or actual_size > MAX_NORMALIZED_BYTES:
            raise PortfolioFormatError("E_NORMALIZED_TOO_LARGE")
        if inspection.source_bytes and actual_size > inspection.source_bytes * MAX_EXPANSION_RATIO:
            raise PortfolioFormatError("E_EXPANSION_LIMIT")
        if normalized_hash != _sha256(normalized):
            raise PortfolioFormatError("E_DECODE_PROTOCOL")
        try:
            normalized.chmod(0o600)
        except OSError:
            pass
        yield NormalizedPortfolio(
            normalized,
            inspection.kind,
            body,
            inspection.source_bytes,
            actual_size,
            source_hash,
            normalized_hash,
        )
    finally:
        shutil.rmtree(private_root, ignore_errors=True)
