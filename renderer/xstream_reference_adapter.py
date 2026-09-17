"""Read-only adapter from XStream ID references to relative XPath references."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET
from typing import Iterator

from .portfolio_xml_model import _ReferenceResolver


def _relative_reference(source_path: str, target_path: str) -> str:
    source = [part for part in source_path.split("/") if part]
    target = [part for part in target_path.split("/") if part]
    common = 0
    for left, right in zip(source, target):
        if left != right:
            break
        common += 1
    return "/".join([".."] * (len(source) - common) + target[common:]) or "."


@contextmanager
def xpath_compatible_xml(source: Path) -> Iterator[Path]:
    """Yield the source or a disposable, semantically equivalent XPath copy."""
    tree = ET.parse(source)
    root = tree.getroot()
    resolver = _ReferenceResolver(root)
    audit = resolver.audit()
    resolver.raise_for_audit(audit)
    if resolver.mode != "id":
        yield source
        return

    resolved = [
        (element, resolver.path(resolver.resolve(element)))
        for element in resolver.reference_elements
    ]
    for element, target_path in resolved:
        element.set(
            "reference",
            _relative_reference(resolver.path(element), target_path),
        )
    for element in root.iter():
        element.attrib.pop("id", None)

    with tempfile.TemporaryDirectory(prefix="pp-id-reference-adapter-") as temporary:
        converted = Path(temporary) / "portfolio.xml"
        tree.write(converted, encoding="utf-8", xml_declaration=True)
        yield converted
