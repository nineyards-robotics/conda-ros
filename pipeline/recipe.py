"""Generate rattler-build v1 recipe.yaml from a package manifest.

The output is a pure function of the package manifest, the release
metadata, the distro snapshot (for ROS-on-ROS resolution), and the
shared rosdep mapping — no network access happens here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .build_config import conda_package_name
from .package_xml import PackageManifest
from .rosdep_mapping import ResolvedDep, resolve
from .rosdistro import DistroSnapshot, PackageRelease

# Implicit deps added per build type. These are things package.xml does
# not typically declare but the build still needs. Values here are a
# best-effort starting point — cross-check against a known-good
# reference recipe once we have one building and correct as needed.
_COMMON_CMAKE_BUILD = [
    "${{ compiler('c') }}",
    "${{ compiler('cxx') }}",
    "cmake",
    "ninja",
    "pkg-config",
]

_IMPLICIT: dict[str, dict[str, list[str]]] = {
    "ament_cmake": {
        "build": list(_COMMON_CMAKE_BUILD),
        "host": ["python"],
        "run": [],
    },
    "ament_python": {
        "build": [],
        "host": ["python", "setuptools", "pip"],
        "run": ["python"],
    },
    "cmake": {
        "build": list(_COMMON_CMAKE_BUILD),
        "host": [],
        "run": [],
    },
}

# Build scripts per build type. TODO: emit Windows variants via
# rattler-build's if/then selectors once we start building win-64.
_CMAKE_SCRIPT = [
    "mkdir -p build && cd build",
    (
        "cmake $SRC_DIR -GNinja"
        " -DCMAKE_INSTALL_PREFIX=$PREFIX"
        " -DCMAKE_BUILD_TYPE=Release"
        " -DBUILD_TESTING=OFF"
    ),
    "cmake --build . --config Release --parallel ${CPU_COUNT:-2}",
    "cmake --install .",
]

_SCRIPTS: dict[str, list[str]] = {
    "ament_cmake": list(_CMAKE_SCRIPT),
    "cmake": list(_CMAKE_SCRIPT),
    "ament_python": [
        "$PYTHON -m pip install . --no-deps --no-build-isolation -vv",
    ],
}

_VENDOR_SCRIPT = ['echo "vendor passthrough — no build"']


@dataclass
class RecipeResult:
    """Outcome of generating a recipe.

    ``path`` is None when the recipe was not written because one or
    more rosdep keys failed to resolve — see ``unknown_keys``.  We
    intentionally don't persist an incomplete recipe: the skip-if-exists
    guard would otherwise make a broken recipe sticky across runs.
    """

    path: Path | None
    unknown_keys: set[str] = field(default_factory=set)


def recipe_path(repo_root: Path, distro: str, name: str, version: str) -> Path:
    """Return the canonical recipe path for a package/version."""
    return (
        repo_root
        / "distros"
        / distro
        / "packages"
        / name
        / version
        / "recipe.yaml"
    )


def _resolve_keys(
    keys: list[str],
    snapshot: DistroSnapshot,
    rosdep_map: dict,
    unknown: set[str],
) -> list[Any]:
    """Resolve rosdep keys into a list ready for YAML emission.

    Strings are unconditional deps.  Dicts carry ``if``/``then`` selectors
    for platform-specific deps.
    """
    out: list[Any] = []
    for key in keys:
        resolved = resolve(key, snapshot, rosdep_map)
        if resolved is None:
            unknown.add(key)
            continue
        for dep in resolved:
            if not dep.names:
                continue
            if dep.platform is None:
                out.extend(dep.names)
            else:
                out.append({"if": dep.platform, "then": list(dep.names)})
    return out


def _dedupe(entries: list[Any]) -> list[Any]:
    """Dedupe unconditional string entries in order; pass dicts through."""
    seen: set[str] = set()
    out: list[Any] = []
    for entry in entries:
        if isinstance(entry, str):
            if entry not in seen:
                seen.add(entry)
                out.append(entry)
        else:
            out.append(entry)
    return out


# SPDX identifiers we recognize directly — license strings that already
# match one of these pass through unchanged.
_SPDX_VALID: set[str] = {
    "Apache-2.0",
    "BSD-1-Clause", "BSD-2-Clause", "BSD-3-Clause",
    "MIT", "MIT-0",
    "MPL-1.1", "MPL-2.0",
    "EPL-2.0",
    "GPL-2.0-only", "GPL-2.0-or-later",
    "GPL-3.0-only", "GPL-3.0-or-later",
    "LGPL-2.1-only", "LGPL-2.1-or-later",
    "LGPL-3.0-only", "LGPL-3.0-or-later",
    "AGPL-3.0-only", "AGPL-3.0-or-later",
    "BSL-1.0", "CC0-1.0", "Zlib", "HPND",
    "CC-BY-3.0", "CC-BY-NC-SA-3.0",
}

# Mapping of normalized freeform license strings -> SPDX identifier.
# Keys are the result of _license_key (lowercase, alphanumeric-only).
# Bare family names default to the most common ROS variant: "BSD" ->
# 3-Clause, "GPL"/"LGPL" -> 3.0-or-later.
_SPDX_ALIASES: dict[str, str] = {
    # Apache 2.0
    "apache": "Apache-2.0",
    "apache2": "Apache-2.0",
    "apache20": "Apache-2.0",
    "apache20license": "Apache-2.0",
    "apachelicense20": "Apache-2.0",
    "apachelicence20": "Apache-2.0",
    "apachelicenseversion20": "Apache-2.0",
    "alv2": "Apache-2.0",
    # BSD
    "bsd": "BSD-3-Clause",
    "bsd2": "BSD-2-Clause",
    "bsd2clause": "BSD-2-Clause",
    "bsd3": "BSD-3-Clause",
    "bsd3clause": "BSD-3-Clause",
    "3clausebsd": "BSD-3-Clause",
    "bsdclause3": "BSD-3-Clause",
    "bsd3clauselicense": "BSD-3-Clause",
    "bsdlicense20": "BSD-3-Clause",
    # MIT
    "mit": "MIT",
    "mitlicense": "MIT",
    "mit0": "MIT-0",
    # GPL
    "gpl": "GPL-3.0-or-later",
    "gplv2": "GPL-2.0-or-later",
    "gplv2license": "GPL-2.0-or-later",
    "gpl20orlater": "GPL-2.0-or-later",
    "gnugeneralpubliclicensev20": "GPL-2.0-or-later",
    "gpl30": "GPL-3.0-or-later",
    "gpl30only": "GPL-3.0-only",
    "gpl30orlater": "GPL-3.0-or-later",
    "gplv3": "GPL-3.0-or-later",
    # LGPL
    "lgpl": "LGPL-3.0-or-later",
    "lgpl21orlater": "LGPL-2.1-or-later",
    "lgplv21": "LGPL-2.1-or-later",
    "gnulesserpubliclicense21": "LGPL-2.1-or-later",
    "lgpl30orlater": "LGPL-3.0-or-later",
    "lgplv3": "LGPL-3.0-or-later",
    # AGPL
    "agplv3": "AGPL-3.0-or-later",
    # MPL / EPL
    "mpl11": "MPL-1.1",
    "mpl20": "MPL-2.0",
    "mpl20license": "MPL-2.0",
    "mozillapubliclicense20": "MPL-2.0",
    "eclipsepubliclicense20": "EPL-2.0",
    # SPDX retired EDL-1.0 — semantically equivalent to BSD-3-Clause.
    "eclipsedistributionlicense10": "BSD-3-Clause",
    # Misc SPDX
    "bsl10": "BSL-1.0",
    "cc0": "CC0-1.0",
    "cc01": "CC0-1.0",
    "cc010": "CC0-1.0",
    "zlib": "Zlib",
    "zliblicense": "Zlib",
    "hpnd": "HPND",
    "publicdomain": "LicenseRef-PublicDomain",
}

# Split compound license strings on " AND " / " and " / commas.
_LICENSE_SPLIT_RE = re.compile(r"\s+and\s+|,\s+", re.IGNORECASE)


def _license_key(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def _normalize_license_part(part: str) -> str:
    s = part.strip()
    if not s:
        return ""
    lowered = s.lower()
    for suffix in (" license", " licence"):
        if lowered.endswith(suffix):
            s = s[: -len(suffix)]
            break
    if s in _SPDX_VALID:
        return s
    return _SPDX_ALIASES.get(_license_key(s), "LicenseRef-Unknown")


def _normalize_license(licenses: list[str]) -> str:
    parts: list[str] = []
    for lic in licenses:
        for chunk in _LICENSE_SPLIT_RE.split(lic):
            normalized = _normalize_license_part(chunk)
            if normalized and normalized not in parts:
                parts.append(normalized)
    return " AND ".join(parts) if parts else "LicenseRef-Unknown"


def _about(manifest: PackageManifest) -> dict[str, Any]:
    about: dict[str, Any] = {}
    if manifest.description:
        about["summary"] = manifest.description
    if manifest.licenses:
        about["license"] = _normalize_license(manifest.licenses)
    if "website" in manifest.urls:
        about["homepage"] = manifest.urls["website"]
    if "repository" in manifest.urls:
        about["repository"] = manifest.urls["repository"]
    return about


def build_recipe(
    manifest: PackageManifest,
    release: PackageRelease,
    snapshot: DistroSnapshot,
    rosdep_map: dict,
    vendor_passthrough: set[str],
    unknown: set[str],
) -> dict[str, Any]:
    """Assemble the recipe dict for a single package."""
    conda_name = conda_package_name(snapshot.distro, release.name)
    is_vendor = release.name in vendor_passthrough

    recipe: dict[str, Any] = {
        "schema_version": 1,
        "package": {"name": conda_name, "version": release.version},
    }

    if not is_vendor:
        recipe["source"] = {
            "git": release.release_url,
            "tag": release.formatted_tag(),
        }

    build: dict[str, Any] = {"number": 0}
    if is_vendor:
        build["noarch"] = "generic"
        build["script"] = list(_VENDOR_SCRIPT)
    else:
        build["script"] = list(
            _SCRIPTS.get(manifest.build_type, _SCRIPTS["cmake"])
        )
    recipe["build"] = build

    implicit = _IMPLICIT.get(manifest.build_type, _IMPLICIT["cmake"])

    build_deps = _resolve_keys(manifest.buildtool_deps, snapshot, rosdep_map, unknown)
    host_deps = _resolve_keys(manifest.build_deps, snapshot, rosdep_map, unknown)
    run_deps = _resolve_keys(manifest.run_deps, snapshot, rosdep_map, unknown)
    # Test deps are parsed but not currently emitted — rattler-build v1
    # requires tests to declare a type (python/script/package_contents
    # etc.), and we don't have a generic test definition to emit yet.
    # Resolve them anyway so unknown-key reporting stays complete.
    _resolve_keys(manifest.test_deps, snapshot, rosdep_map, unknown)

    if not is_vendor:
        build_deps = build_deps + list(implicit["build"])
        host_deps = host_deps + list(implicit["host"])
        run_deps = run_deps + list(implicit["run"])

    requirements: dict[str, Any] = {}
    if is_vendor:
        # Vendor packages are pure metadata — only run deps matter.
        if run_deps:
            requirements["run"] = _dedupe(run_deps)
    else:
        if build_deps:
            requirements["build"] = _dedupe(build_deps)
        if host_deps:
            requirements["host"] = _dedupe(host_deps)
        if run_deps:
            requirements["run"] = _dedupe(run_deps)
    if requirements:
        recipe["requirements"] = requirements

    about = _about(manifest)
    if about:
        recipe["about"] = about

    return recipe


def write_recipe(
    recipe: dict[str, Any],
    release: PackageRelease,
    repo_root: Path,
    distro: str,
) -> Path:
    """Write *recipe* to its canonical path. Returns the path."""
    path = recipe_path(repo_root, distro, release.name, release.version)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.dump(recipe, f, sort_keys=False, default_flow_style=False)
    return path


def generate(
    release: PackageRelease,
    manifest: PackageManifest,
    snapshot: DistroSnapshot,
    rosdep_map: dict,
    vendor_passthrough: set[str],
    repo_root: Path,
) -> RecipeResult:
    """Generate and conditionally write a recipe.

    If any rosdep key fails to resolve, the recipe is not written and
    the unknown keys are returned instead — so a re-run after editing
    rosdep.yaml will produce the recipe.
    """
    unknown: set[str] = set()
    recipe = build_recipe(
        manifest, release, snapshot, rosdep_map, vendor_passthrough, unknown
    )
    if unknown:
        return RecipeResult(path=None, unknown_keys=unknown)
    path = write_recipe(recipe, release, repo_root, snapshot.distro)
    return RecipeResult(path=path, unknown_keys=unknown)
