"""Read group names from a C-Bus Toolkit project (.cbz) or C-Gate tag file (.xml)."""
from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass
class ProjectUnit:
    """A unit (device) on the C-Bus network, from a project file."""

    address: int
    name: str
    unit_type: str | None
    catalog: str | None

    @property
    def is_input(self) -> bool:
        """Key input units (wall switches, DLTs) - their presses are events."""
        kind = (self.unit_type or "").upper()
        return kind.startswith(("KEY", "DLT"))


@dataclass
class Project:
    """Names read from a project file."""

    groups: dict[int, dict[int, str]] = field(default_factory=dict)
    units: dict[int, ProjectUnit] = field(default_factory=dict)


def _pretty(name: str) -> str:
    """'SPIRAL_B' -> 'Spiral B'; names that are not all capitals are kept."""
    if name.isupper():
        return name.replace("_", " ").title()
    return name


def parse_project_units(data: bytes) -> dict[int, ProjectUnit]:
    """Return {unit address: ProjectUnit} from C-Gate / Toolkit XML."""
    root = ET.fromstring(data)
    units: dict[int, ProjectUnit] = {}
    for unit in root.iter("Unit"):
        try:
            addr = int(_text(unit, "Address") or "")
        except ValueError:
            continue
        name = _text(unit, "TagName") or _text(unit, "UnitName")
        if not name or not 0 <= addr <= 255:
            continue
        units[addr] = ProjectUnit(
            addr, _pretty(name), _text(unit, "UnitType"), _text(unit, "CatalogNumber")
        )
    return units


def load_project(path: str) -> Project:
    """Load group and unit names from a .xml or .cbz file. Blocking."""
    file = Path(path)
    if zipfile.is_zipfile(file):
        with zipfile.ZipFile(file) as archive:
            members = [archive.read(m) for m in archive.namelist() if m.lower().endswith(".xml")]
    else:
        members = [file.read_bytes()]
    project = Project()
    for data in members:
        for app, groups in parse_project_xml(data).items():
            project.groups.setdefault(app, {}).update(groups)
        project.units.update(parse_project_units(data))
    return project
