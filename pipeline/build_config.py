"""Generate snapshot conda_build_config.yaml.

Step 2 of the build pipeline: merge conda-forge system pins, local
overrides, and ROS package versions into a per-snapshot variant config
that rattler-build uses for version resolution and build hash
computation.

Layering order (later wins):
    1. conda-forge-pinning (ecosystem-wide pins)
    2. Local conda_build_config.yaml (our overrides)
    3. ROS package versions from distribution.yaml
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .rosdistro import DistroSnapshot


def conda_package_name(distro: str, ros_name: str) -> str:
    """Convert a ROS package name to its conda package name.

    Replaces underscores with hyphens and prepends the distro prefix:
        rclcpp -> ros-jazzy-rclcpp
        nav2_bringup -> ros-jazzy-nav2-bringup
    """
    return f"ros-{distro}-{ros_name.replace('_', '-')}"


def generate_snapshot_build_config(
    snapshot: DistroSnapshot,
    conda_forge_pins: dict,
    conda_forge_pinning_commit: str,
    local_overrides_path: Path,
    output_path: Path,
) -> Path:
    """Write the snapshot conda_build_config.yaml.

    Layers conda-forge-pinning, local overrides, and ROS package
    versions.  Each ROS entry is a single-element list keyed by conda
    package name.

    Returns the path written.
    """
    # Start with conda-forge-pinning
    config: dict = dict(conda_forge_pins)

    # Layer local overrides on top
    if local_overrides_path.exists():
        text = local_overrides_path.read_text()
        if text.strip():
            overrides = yaml.safe_load(text) or {}
            config.update(overrides)

    # Add ROS package versions
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
