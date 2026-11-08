"""Read group names from a C-Bus Toolkit project (.cbz) or C-Gate tag file (.xml)."""
from __future__ import annotations

import logging
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

_LOGGER = logging.getLogger(__name__)


def _text(node: ET.Element, tag: str) -> str | None:
    child = node.find(tag)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def parse_project_xml(data: bytes) -> dict[int, dict[int, str]]:
    """Return {application: {group: name}} from C-Gate / Toolkit XML."""
    root = ET.fromstring(data)
    result: dict[int, dict[int, str]] = {}
    for app in root.iter("Application"):
        try:
            app_addr = int(_text(app, "Address") or "")
        except ValueError:
            continue
        groups: dict[int, str] = {}
        for group in app.findall("Group"):
            name = _text(group, "TagName")
            try:
                addr = int(_text(group, "Address") or "")
            except ValueError:
                continue
            if addr == 255 or not name or name == "<Unused>":
                continue
            groups[addr] = name
        if groups:
            result.setdefault(app_addr, {}).update(groups)
    return result


def load_project_file(path: str) -> dict[int, dict[int, str]]:
    """Load names from a .xml or .cbz file. Blocking."""
    file = Path(path)
    if zipfile.is_zipfile(file):
        with zipfile.ZipFile(file) as archive:
            merged: dict[int, dict[int, str]] = {}
            for member in archive.namelist():
                if member.lower().endswith(".xml"):
                    for app, groups in parse_project_xml(archive.read(member)).items():
                        merged.setdefault(app, {}).update(groups)
            return merged
    return parse_project_xml(file.read_bytes())
