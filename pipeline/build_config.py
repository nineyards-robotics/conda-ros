"""Generate snapshot conda_build_config.yaml.

Step 3 of the build pipeline: merge conda-forge system pins, local
overrides, resolved versions for deps that aren't otherwise pinned, and
ROS package versions into a per-snapshot variant config that
rattler-build uses for version resolution and build hash computation.

Layering order (later wins):
    1. conda-forge-pinning (ecosystem-wide pins)
    2. Local conda_build_config.yaml (our overrides)
    3. Resolved dependency versions (current conda-forge versions for
       deps referenced by recipes but not pinned by layers 1 or 2)
    4. ROS package versions from distribution.yaml
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import yaml

from .rosdistro import DistroSnapshot


def conda_package_name(distro: str, ros_name: str) -> str:
    """Convert a ROS package name to its conda package name.

    Replaces underscores with hyphens and prepends the distro prefix:
        rclcpp -> ros-jazzy-rclcpp
        nav2_bringup -> ros-jazzy-nav2-bringup
    """
    return f"ros-{distro}-{ros_name.replace('_', '-')}"


def _recipe_path(repo_root: Path, distro: str, name: str, version: str) -> Path:
    return (
        repo_root
        / "distros"
        / distro
        / "packages"
        / name
        / version
        / "recipe.yaml"
    )


def _collect_names(entry: Any, out: set[str]) -> None:
    """Extract bare package names from a requirements entry."""
    if isinstance(entry, str):
        # Skip jinja templates like ${{ compiler('c') }}.
        if entry.startswith("$"):
            return
        token = entry.split()[0] if entry.strip() else ""
        if token:
            out.add(token)
    elif isinstance(entry, dict):
        # Selector form: {if: ..., then: [...]}.
        for sub in entry.get("then") or []:
            _collect_names(sub, out)
        # Some selector forms also carry "else".
        for sub in entry.get("else") or []:
            _collect_names(sub, out)


def scan_recipe_deps(
    repo_root: Path,
    distro: str,
    snapshot: DistroSnapshot,
) -> set[str]:
    """Return unique non-ROS dep names across all recipe.yaml files in
    the snapshot.

    ROS packages are excluded since their versions are added by layer 4.
    Jinja template entries (``${{ ... }}``) are excluded. Missing recipe
    files are silently skipped so this can run against a partial set.
    """
    ros_prefix = f"ros-{distro}-"
    deps: set[str] = set()
    for release in snapshot.packages.values():
        path = _recipe_path(repo_root, distro, release.name, release.version)
        if not path.exists():
            continue
        data = yaml.safe_load(path.read_text()) or {}
        reqs = data.get("requirements") or {}
        for section in ("build", "host", "run"):
            for entry in reqs.get(section) or []:
                _collect_names(entry, deps)
        for test in data.get("tests") or []:
            test_reqs = test.get("requirements") or {}
            for entry in test_reqs.get("run") or []:
                _collect_names(entry, deps)
    return {d for d in deps if not d.startswith(ros_prefix)}


def _pinned_keys(pins: dict, overrides: dict) -> set[str]:
    return set(pins.keys()) | set(overrides.keys())


def load_local_overrides(path: Path) -> dict:
    """Load the root conda_build_config.yaml. Returns {} if empty or absent."""
    if not path.exists():
        return {}
    text = path.read_text()
    if not text.strip():
        return {}
    return yaml.safe_load(text) or {}


def generate_snapshot_build_config(
    snapshot: DistroSnapshot,
    conda_forge_pins: dict,
    conda_forge_pinning_commit: str,
    local_overrides: dict,
    resolved_deps: dict[str, str],
    output_path: Path,
) -> Path:
    """Write the snapshot conda_build_config.yaml.

    Layers conda-forge-pinning, local overrides, resolved dep versions,
    and ROS package versions. Each non-CF-pinning entry is a
    single-element list so rattler-build treats it as a variant key.

    Returns the path written.
    """
    config: dict = dict(conda_forge_pins)
    config.update(local_overrides)

    for name in sorted(resolved_deps):
        config[name] = [resolved_deps[name]]

    for pkg in sorted(snapshot.packages.values(), key=lambda p: p.name):
        key = conda_package_name(snapshot.distro, pkg.name)
        config[key] = [pkg.version]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(
            "# Auto-generated snapshot build config.\n"
            f"# rosdistro: {snapshot.distro}  ref: {snapshot.ref}\n"
            f"# conda-forge-pinning: {conda_forge_pinning_commit}\n\n"
        )
        yaml.dump(config, f, default_flow_style=False, sort_keys=True)

    return output_path


def resolve_unpinned(
    scanned: Iterable[str],
    pins: dict,
    overrides: dict,
    latest: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    """Filter scanned deps against existing pins and resolve the rest.

    Returns ``(resolved, missing)`` where ``resolved`` maps name to
    version and ``missing`` lists names with no conda-forge entry.
    """
    already = _pinned_keys(pins, overrides)
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for name in sorted(set(scanned) - already):
        version = latest.get(name)
        if version:
            resolved[name] = version
        else:
            missing.append(name)
    return resolved, missing
