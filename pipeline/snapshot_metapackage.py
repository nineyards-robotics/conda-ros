"""Step 6: generate the snapshot constraint metapackage.

For each target platform, emits a `ros-{distro}-snapshot-{date}` package
whose `run_constrained` entries pin every package in the snapshot's
`distribution.yaml` to its exact `version=buildstring` for that platform.

The package set is driven by `distribution.yaml` at the snapshot ref —
not by whatever happens to be in the channel.  Buildstrings are looked
up from `repodata.json` of every channel passed in, including the local
output dir from Step 5 (which rattler-build also indexes as a channel).

Each platform gets its own recipe file because buildstrings differ
across platforms.  Built artifacts are themselves per-subdir packages —
noarch packages live under the `noarch` subdir and are visible to every
target platform.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .build_config import conda_package_name
from .rosdistro import DistroSnapshot

NOARCH = "noarch"


@dataclass(frozen=True)
class BuildEntry:
    """One build of one package on one subdir."""

    name: str
    version: str
    build_string: str
    build_number: int
    subdir: str
    timestamp: int = 0  # ms since epoch; 0 if missing in repodata


@dataclass
class ResolvedConstraints:
    """Per-platform pin set + accounting of which snapshot packages are missing."""

    platform: str
    constraints: list[BuildEntry]
    missing: list[tuple[str, str]]  # (conda_name, version) pairs we couldn't find


def _read_text(channel: str, subdir: str, filename: str) -> str | None:
    """Read `{channel}/{subdir}/{filename}` from a local path or URL.

    Returns None if the file/URL doesn't exist.  Other errors propagate.
    """
    parsed = urlparse(channel)
    if parsed.scheme in ("", "file"):
        base = Path(parsed.path) if parsed.scheme == "file" else Path(channel)
        path = base / subdir / filename
        if not path.exists():
            return None
        return path.read_text()

    url = f"{channel.rstrip('/')}/{subdir}/{filename}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def fetch_repodata(channel: str, subdir: str) -> dict | None:
    """Fetch repodata.json for a channel/subdir.  None if absent."""
    raw = _read_text(channel, subdir, "repodata.json")
    if raw is None:
        return None
    return json.loads(raw)


def index_repodata(repodata: dict) -> list[BuildEntry]:
    """Flatten repodata.json's package records into BuildEntry objects.

    Reads both the legacy `packages` (.tar.bz2) and modern `packages.conda`
    sections — same schema, different extension.
    """
    out: list[BuildEntry] = []
    subdir = repodata.get("info", {}).get("subdir", "")
    for section in ("packages", "packages.conda"):
        for record in (repodata.get(section) or {}).values():
            out.append(
                BuildEntry(
                    name=record["name"],
                    version=record["version"],
                    build_string=record["build"],
                    build_number=int(record.get("build_number", 0)),
                    subdir=record.get("subdir", subdir),
                    timestamp=int(record.get("timestamp", 0)),
                )
            )
    return out


def collect_builds(channels: list[str], subdirs: list[str]) -> list[BuildEntry]:
    """Read repodata.json from every (channel, subdir) and concatenate.

    Missing repodata files are silently skipped — a channel that hasn't
    been populated for a given subdir is normal.
    """
    all_builds: list[BuildEntry] = []
    for channel in channels:
        for subdir in subdirs:
            repodata = fetch_repodata(channel, subdir)
            if repodata is None:
                continue
            all_builds.extend(index_repodata(repodata))
    return all_builds


def _pick_best(candidates: list[BuildEntry]) -> BuildEntry:
    """Highest build_number wins; ties broken by latest timestamp."""
    return max(candidates, key=lambda b: (b.build_number, b.timestamp))


def resolve_constraints(
    snapshot: DistroSnapshot,
    builds: list[BuildEntry],
    target_platform: str,
) -> ResolvedConstraints:
    """For each package in `snapshot`, pick the best matching build.

    Considers builds from `target_platform` and `noarch`.  A package
    counts as "missing" if no build at the snapshot's exact version
    exists on either subdir.  Packages outside our distro prefix are
    ignored — repodata may include unrelated packages.
    """
    relevant_subdirs = {target_platform, NOARCH}
    by_key: dict[tuple[str, str], list[BuildEntry]] = {}
    for b in builds:
        if b.subdir not in relevant_subdirs:
            continue
        by_key.setdefault((b.name, b.version), []).append(b)

    constraints: list[BuildEntry] = []
    missing: list[tuple[str, str]] = []
    for release in sorted(snapshot.packages.values(), key=lambda p: p.name):
        cname = conda_package_name(snapshot.distro, release.name)
        candidates = by_key.get((cname, release.version))
        if not candidates:
            missing.append((cname, release.version))
            continue
        constraints.append(_pick_best(candidates))

    constraints.sort(key=lambda b: b.name)
    return ResolvedConstraints(
        platform=target_platform, constraints=constraints, missing=missing
    )


def metapackage_name(distro: str, date: str) -> str:
    return f"ros-{distro}-snapshot-{date}"


def metapackage_version(date: str) -> str:
    """Convert YYYY-MM-DD to a conda-legal version (no hyphens)."""
    return date.replace("-", ".")


def build_recipe(
    distro: str,
    date: str,
    resolved: ResolvedConstraints,
    build_number: int = 0,
) -> dict:
    """Construct the recipe.yaml dict for one platform's metapackage.

    The package is empty — no source, no real build script — just
    `run_constrained` entries.  rattler-build still requires a `script`
    field; an empty list is the conventional no-op.
    """
    constraints = [
        f"{b.name} =={b.version} {b.build_string}" for b in resolved.constraints
    ]
    return {
        "schema_version": 1,
        "package": {
            "name": metapackage_name(distro, date),
            "version": metapackage_version(date),
        },
        "build": {
            "number": build_number,
            "script": [],
            "skip": [f'target_platform != "{resolved.platform}"'],
        },
        "requirements": {"run_constraints": constraints},
        "about": {
            "summary": (
                f"Snapshot constraint metapackage for ROS {distro} @ {date}. "
                "Pins every ROS package to the exact build from this snapshot."
            ),
        },
    }


def write_recipe(recipe: dict, stage_dir: Path) -> Path:
    """Write recipe.yaml under stage_dir and return its path."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    path = stage_dir / "recipe.yaml"
    with path.open("w") as f:
        yaml.dump(recipe, f, sort_keys=False, default_flow_style=False)
    return path
