"""Parse ROS package.xml manifests.

Extracts dependency information and build type into a PackageManifest
that the recipe generator can consume without any further XML knowledge.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

DEFAULT_BUILD_TYPE = "ament_cmake"
KNOWN_BUILD_TYPES = {"ament_cmake", "ament_python", "cmake"}


@dataclass
class PackageManifest:
    """Structured view of a package.xml."""

    name: str
    version: str
    build_type: str
    buildtool_deps: list[str] = field(default_factory=list)
    build_deps: list[str] = field(default_factory=list)
    run_deps: list[str] = field(default_factory=list)
    test_deps: list[str] = field(default_factory=list)
    description: str = ""
    licenses: list[str] = field(default_factory=list)
    urls: dict[str, str] = field(default_factory=dict)


def _text(elem: ET.Element | None) -> str:
    if elem is None or elem.text is None:
        return ""
    return elem.text.strip()


def _collect(root: ET.Element, tag: str) -> list[str]:
    out: list[str] = []
    for elem in root.findall(tag):
        value = _text(elem)
        if value:
            out.append(value)
    return out


def _dedupe(items: list[str]) -> list[str]:
    """Preserve first-seen order while removing duplicates."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def parse_package_xml(xml_bytes: bytes) -> PackageManifest:
    """Parse a package.xml document into a PackageManifest.

    Dependency tags map to conda requirement sections as follows:
        buildtool_depend, buildtool_export_depend -> buildtool
        build_depend                              -> build (host)
        build_export_depend                       -> build + run
        depend                                    -> build + run
        exec_depend, run_depend (legacy)          -> run
        test_depend                               -> test
    """
    root = ET.fromstring(xml_bytes)

    name = _text(root.find("name"))
    version = _text(root.find("version"))
    description = _text(root.find("description"))

    # Build type from <export><build_type>
    build_type = DEFAULT_BUILD_TYPE
    export = root.find("export")
    if export is not None:
        bt = _text(export.find("build_type"))
        if bt:
            build_type = bt

    # Combine dep categories per the mapping above.
    buildtool = _collect(root, "buildtool_depend") + _collect(
        root, "buildtool_export_depend"
    )

    depend = _collect(root, "depend")
    build_export = _collect(root, "build_export_depend")

    build = _collect(root, "build_depend") + depend + build_export
    run = (
        _collect(root, "exec_depend")
        + _collect(root, "run_depend")
        + depend
        + build_export
    )
    test = _collect(root, "test_depend")

    licenses = _collect(root, "license")

    urls: dict[str, str] = {}
    for elem in root.findall("url"):
        kind = elem.get("type", "website")
        value = _text(elem)
        if value and kind not in urls:
            urls[kind] = value

    return PackageManifest(
        name=name,
        version=version,
        build_type=build_type,
        buildtool_deps=_dedupe(buildtool),
        build_deps=_dedupe(build),
        run_deps=_dedupe(run),
        test_deps=_dedupe(test),
        description=description,
        licenses=licenses,
        urls=urls,
    )
