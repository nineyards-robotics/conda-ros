"""Fetch and parse rosdistro distribution.yaml.

Step 1 of the build pipeline: retrieve the package index for a ROS2
distribution at a specific git ref and extract release metadata for
every package.
"""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass, field

import yaml

ROSDISTRO_RAW_URL = "https://raw.githubusercontent.com/ros/rosdistro"


@dataclass
class PackageRelease:
    """Release metadata for a single ROS package."""

    name: str  # ROS package name (e.g. "rclcpp")
    version: str  # upstream version without debian revision (e.g. "28.1.5")
    release_url: str  # git URL of the release repo
    release_tag: str  # tag template (e.g. "release/jazzy/{package}/{version}")
    repo_name: str  # repository name in rosdistro


@dataclass
class DistroSnapshot:
    """Parsed snapshot of a rosdistro distribution.yaml."""

    distro: str
    ref: str  # git ref used to fetch
    packages: dict[str, PackageRelease] = field(default_factory=dict)


def fetch_distribution_yaml(distro: str, ref: str = "master") -> dict:
    """Fetch distribution.yaml from ros/rosdistro at *ref*."""
    url = f"{ROSDISTRO_RAW_URL}/{ref}/{distro}/distribution.yaml"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return yaml.safe_load(resp.read())


def strip_debian_revision(version: str) -> str:
    """Strip debian revision suffix. '2.5.0-1' -> '2.5.0'."""
    if "-" in version:
        return version.rsplit("-", 1)[0]
    return version


def parse_distribution(
    distro: str,
    dist_yaml: dict,
    ref: str = "",
) -> DistroSnapshot:
    """Parse distribution.yaml into a DistroSnapshot.

    Only repositories with a ``release`` section and a version are included.
    """
    snapshot = DistroSnapshot(distro=distro, ref=ref)
    repositories = dist_yaml.get("repositories", {})

    for repo_name, repo_data in repositories.items():
        release = repo_data.get("release")
        if not release:
            continue

        version_str = release.get("version")
        if not version_str:
            continue

        url = release.get("url", "")
        tag_format = release.get("tags", {}).get(
            "release", "release/{package}/{version}"
        )
        version = strip_debian_revision(version_str)

        # Multi-package repos list packages explicitly; single-package repos
        # use the repository name as the implicit package name.
        pkg_names = release.get("packages", [repo_name])

        for pkg_name in pkg_names:
            snapshot.packages[pkg_name] = PackageRelease(
                name=pkg_name,
                version=version,
                release_url=url,
                release_tag=tag_format,
                repo_name=repo_name,
            )

    return snapshot
