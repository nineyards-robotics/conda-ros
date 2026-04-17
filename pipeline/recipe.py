"""Generate rattler-build v1 recipe.yaml from a package manifest.

The output is a pure function of the package manifest, the release
metadata, the distro snapshot (for ROS-on-ROS resolution), and the
shared rosdep mapping — no network access happens here.
"""

from __future__ import annotations

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


def _about(manifest: PackageManifest) -> dict[str, Any]:
    about: dict[str, Any] = {}
    if manifest.description:
        about["summary"] = manifest.description
    if manifest.licenses:
        # Multiple licenses joined with AND. Not SPDX-compliant in every
        # case but a reasonable default; recipes can be hand-edited.
        about["license"] = " AND ".join(manifest.licenses)
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
    test_deps = _resolve_keys(manifest.test_deps, snapshot, rosdep_map, unknown)

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

    if test_deps:
        recipe["tests"] = [
            {"requirements": {"run": _dedupe(test_deps)}}
        ]

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
