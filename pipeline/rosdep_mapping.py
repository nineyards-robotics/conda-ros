"""Resolve rosdep keys to conda package names.

A rosdep key is resolved in two steps:

1. If the key is a ROS package in the current snapshot, it maps to the
   corresponding conda package name (`ros-{distro}-{name}`).
2. Otherwise it is looked up in the root ``rosdep.yaml`` mapping file.

Unknown keys are reported to the caller rather than raising so that the
orchestrator can surface them in a single summary at the end of a run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .build_config import conda_package_name
from .rosdistro import DistroSnapshot

# Platforms recognised in the rosdep.yaml per-key overrides. Anything else
# is treated as a conda selector key passed straight through.
_PLATFORMS = {"linux", "osx", "win", "unix"}


@dataclass(frozen=True)
class ResolvedDep:
    """A single resolved dependency entry.

    ``platform`` is None for unconditional deps, otherwise one of the
    keys in ``_PLATFORMS``.  ``names`` may be empty, which encodes a
    deliberate "skip this key on this platform" decision.
    """

    names: tuple[str, ...]
    platform: str | None = None


def load_rosdep(path: Path) -> dict:
    """Load the root rosdep.yaml. Returns {} if the file is empty."""
    if not path.exists():
        return {}
    text = path.read_text()
    if not text.strip():
        return {}
    return yaml.safe_load(text) or {}


def _normalise(value) -> tuple[str, ...]:
    """Coerce a mapping value into a tuple of conda package names."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(item) for item in value if item)
    raise TypeError(f"unsupported rosdep mapping value: {value!r}")


def _resolve_system(key: str, entry) -> list[ResolvedDep]:
    """Resolve a system rosdep entry from rosdep.yaml."""
    # String / list shorthand: applies to all platforms.
    if entry is None or isinstance(entry, (str, list)):
        names = _normalise(entry)
        return [ResolvedDep(names=names)] if names else []

    if not isinstance(entry, dict):
        raise TypeError(f"unsupported rosdep entry for {key!r}: {entry!r}")

    # "conda:" wrapper is accepted for future-proofing — rosdep upstream
    # uses per-package-manager keys. We only care about conda.
    if "conda" in entry:
        return _resolve_system(key, entry["conda"])

    out: list[ResolvedDep] = []
    for platform_key, value in entry.items():
        # Silently skip platforms we don't target (e.g. emscripten,
        # freebsd). New platforms need to be added to _PLATFORMS to
        # opt them in.
        if platform_key not in _PLATFORMS:
            continue
        names = _normalise(value)
        if names:
            out.append(ResolvedDep(names=names, platform=platform_key))
    return out


def resolve(
    key: str,
    snapshot: DistroSnapshot,
    rosdep_map: dict,
) -> list[ResolvedDep] | None:
    """Resolve a rosdep key.

    Returns a list of ResolvedDep entries, or None if the key cannot
    be resolved (caller should log and skip).  An empty list is a
    valid result for keys that deliberately resolve to nothing.
    """
    # ROS packages resolve directly to their conda names.
    if key in snapshot.packages:
        name = conda_package_name(snapshot.distro, key)
        return [ResolvedDep(names=(name,))]

    if key in rosdep_map:
        return _resolve_system(key, rosdep_map[key])

    return None
