"""Generate snapshot variant configuration files.

Step 3 of the build pipeline emits two files under
``distros/{distro}/snapshots/{date}/``:

    pinning/conda_build_config.yaml
        Raw passthrough of upstream conda-forge-pinning at a specific
        commit.  Platform selector comments (``# [linux64]``,
        ``# [armv7l]``, etc.) are preserved verbatim so rattler-build
        can subset per target platform at build time.  Never written
        to by hand — regenerate the snapshot to bump the pinning
        commit.

    conda_build_config.yaml
        Our additions on top of conda-forge-pinning:
            - local overrides (from the repo-root conda_build_config.yaml)
            - resolved dep versions (conda-forge latest for deps
              referenced by recipes but not otherwise pinned)
            - ROS package versions from distribution.yaml
        All values are flat lists — no selectors — so this file is
        inherently single-platform-agnostic for the keys it touches.

Both files are named ``conda_build_config.yaml`` because
rattler-build only parses legacy ``# [selector]`` comments in files
with that exact basename — the upstream passthrough relies on those
selectors, so it has to sit in a subdir to avoid colliding with ours.

Step 5 passes both files via repeated ``-m`` / ``--variant-config``
flags, in that order.  rattler-build treats later ``-m`` as overriding
earlier ones, so our additions win over anything in upstream.  That
lets the repo-root ``conda_build_config.yaml`` override individual
conda-forge-pinning entries cleanly, without any textual manipulation
of the upstream file.
"""

from __future__ import annotations

import re
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


def _as_variant_list(value: Any) -> list:
    """Normalize a variant value to a list.

    rattler-build rejects scalar variant values (``key: foo``) with an
    "expected a sequence" error.  We only emit our own layers (which
    may have scalars in local overrides), so this is used on the
    non-upstream file.  The raw upstream file keeps scalars that carry
    platform selectors — rattler-build filters them correctly.
    """
    if isinstance(value, list):
        return value
    return [value]


PINNING_SUBDIR = "pinning"
CBC_FILENAME = "conda_build_config.yaml"


# Top-level key-with-scalar-value lines in conda-forge-pinning.  The
# character class in the value excludes anything that'd be a list/map
# opener or a comment so we don't misfire on `key: [1, 2]` or
# `key: {a: b}` or blank keys.
_TOP_LEVEL_SCALAR = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*):[ \t]+([^\s#\-\[\{][^#]*?)(\s*#.*)?$"
)


# Top-level keys we drop from upstream conda-forge-pinning before
# handing it to rattler-build.  Each entry here has a concrete reason:
#
#   zip_keys
#       Declares groups of variant keys that must be chosen together
#       by index.  Upstream relies on a full conda-smithy-grade
#       selector pass (including ``os.environ.get(...)`` selectors)
#       that rattler-build does not implement, leaving zip members
#       with mismatched lengths.  In practice dropping this doesn't
#       multiply builds: ROS recipes use ``${{ compiler('c') }}``
#       which resolves the toolchain internally, so no recipe
#       references zip-grouped keys as plain variants.
#
#   channel_sources / channel_targets
#       conda-smithy hints telling feedstock CI which channels to
#       pull from and publish to.  rattler-build rejects them when
#       ``--channel`` is also set on the CLI, and we set ``--channel``
#       in Step 5 so the remote and local-output channels are
#       available for dep resolution.
#
# If we ever need real support for one of these, the right fix is to
# pull in a proper selector evaluator (conda-build / conda-smithy)
# rather than partially reimplementing it here.
_DROP_TOP_LEVEL_KEYS: tuple[str, ...] = (
    "zip_keys",
    "channel_sources",
    "channel_targets",
)


def _strip_top_level_blocks(raw: str, keys: Iterable[str]) -> str:
    """Drop the named top-level keys and their bodies from ``raw``.

    Matches a line starting with ``{key}:`` at column 0 and skips
    every subsequent indented or blank line until the next non-blank
    line at column 0.  That next line is then itself re-evaluated —
    so back-to-back drop blocks (e.g. ``channel_sources`` immediately
    followed by ``channel_targets``) are both removed.
    """
    drop = tuple(f"{k}:" for k in keys)
    out: list[str] = []
    dropping = False
    for line in raw.splitlines():
        if dropping:
            if not line or line[0].isspace():
                continue
            dropping = False  # fall through — re-check this new line
        if any(line.startswith(prefix) for prefix in drop):
            dropping = True
            continue
        out.append(line)
    tail = "\n" if raw.endswith("\n") else ""
    return "\n".join(out) + tail


def _wrap_top_level_scalars(raw: str) -> str:
    """Rewrite top-level scalar entries into single-element lists.

    rattler-build validates the shape of every top-level variant value
    before applying ``# [selector]`` comments, so a line like
    ``cdt_arch: armv7l  # [armv7l]`` is rejected as a scalar on
    linux-64 even though the selector would exclude it.  Rewriting to::

        cdt_arch:  # [armv7l]
          - armv7l

    keeps the selector on the key (same filtering semantics) while
    giving rattler-build the list shape it demands.  Nested mappings
    (children of ``pin_run_as_build`` etc.) stay as-is because the
    regex only anchors at column 0.
    """
    out: list[str] = []
    for line in raw.splitlines():
        m = _TOP_LEVEL_SCALAR.match(line)
        if not m:
            out.append(line)
            continue
        key, value, comment = m.group(1), m.group(2).rstrip(), m.group(3) or ""
        out.append(f"{key}:{comment}")
        out.append(f"  - {value}")
    tail = "\n" if raw.endswith("\n") else ""
    return "\n".join(out) + tail


def snapshot_config_paths(output_dir: Path) -> list[Path]:
    """Return the variant-config paths for a snapshot in rattler-build order.

    The first entry is the upstream passthrough (in ``pinning/`` so it
    can share the ``conda_build_config.yaml`` basename that
    rattler-build needs for legacy selector parsing); the second is
    our layered additions.  Step 5 passes these to rattler-build as
    repeated ``-m`` flags in this order so later-wins semantics put
    our values on top of upstream.
    """
    return [
        output_dir / PINNING_SUBDIR / CBC_FILENAME,
        output_dir / CBC_FILENAME,
    ]


def generate_snapshot_build_config(
    snapshot: DistroSnapshot,
    conda_forge_pinning_raw: str,
    conda_forge_pinning_commit: str,
    local_overrides: dict,
    resolved_deps: dict[str, str],
    output_dir: Path,
) -> list[Path]:
    """Write the snapshot variant config files.

    Two files are emitted (see module docstring for the rationale):

        ``conda_forge_pinning.yaml`` is the raw upstream text with a
        small provenance header prepended.  Selector comments are
        preserved so rattler-build can subset per target platform.

        ``conda_build_config.yaml`` holds our overrides, resolved dep
        versions, and ROS package versions — all as flat lists.

    Returns the paths in rattler-build ``-m`` order.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    pinning_path, our_path = snapshot_config_paths(output_dir)
    pinning_path.parent.mkdir(parents=True, exist_ok=True)

    pinning_header = (
        "# Auto-generated — DO NOT EDIT.  Upstream conda-forge-pinning\n"
        f"# rosdistro: {snapshot.distro}  ref: {snapshot.ref}\n"
        f"# conda-forge-pinning commit: {conda_forge_pinning_commit}\n"
        "# Kept near-verbatim so rattler-build sees '# [selector]'\n"
        "# comments.  Top-level scalar entries are wrapped as single-\n"
        "# element lists to satisfy rattler-build's shape check —\n"
        "# see _wrap_top_level_scalars for details.\n"
        "#\n"
        "# Named conda_build_config.yaml (in a subdir to avoid a\n"
        "# name clash with the parent dir's file) because rattler-\n"
        "# build only honours legacy `# [selector]` comments in\n"
        "# variant configs with that exact basename.\n"
        "#\n"
        "# This is layered under ../conda_build_config.yaml —\n"
        "# duplicate keys in the parent file win.\n\n"
    )
    pinning_body = _wrap_top_level_scalars(
        _strip_top_level_blocks(conda_forge_pinning_raw, _DROP_TOP_LEVEL_KEYS)
    )
    pinning_path.write_text(pinning_header + pinning_body)

    our_config: dict = dict(local_overrides)
    for name in sorted(resolved_deps):
        our_config[name] = [resolved_deps[name]]
    for pkg in sorted(snapshot.packages.values(), key=lambda p: p.name):
        key = conda_package_name(snapshot.distro, pkg.name)
        our_config[key] = [pkg.version]
    our_config = {k: _as_variant_list(v) for k, v in our_config.items()}

    with open(our_path, "w") as f:
        f.write(
            "# Auto-generated snapshot overrides.\n"
            f"# rosdistro: {snapshot.distro}  ref: {snapshot.ref}\n"
            f"# conda-forge-pinning commit: {conda_forge_pinning_commit}\n"
            "# Layered on top of conda_forge_pinning.yaml via repeated\n"
            "# `-m` flags to rattler-build — later wins on duplicate keys.\n\n"
        )
        yaml.dump(our_config, f, default_flow_style=False, sort_keys=True)

    return [pinning_path, our_path]


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
