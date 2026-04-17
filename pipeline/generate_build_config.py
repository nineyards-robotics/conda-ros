"""Orchestrator for Step 3: generate the snapshot variant config files.

Fetches rosdistro at the snapshot ref, scans existing recipes for
non-ROS dependency names, looks up current versions on conda-forge for
any not already pinned, and writes two variant config files under
``distros/{distro}/snapshots/{date}/`` — see ``pipeline.build_config``
for the layering rationale.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .build_config import (
    generate_snapshot_build_config,
    load_local_overrides,
    resolve_unpinned,
    scan_recipe_deps,
)
from .channeldata import fetch_channeldata, latest_versions
from .conda_forge_pinning import fetch_pinning
from .rosdistro import fetch_distribution_yaml, parse_distribution

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class BuildConfigSummary:
    output_paths: list[Path] = field(default_factory=list)
    pinning_commit: str = ""
    scanned: int = 0
    already_pinned: int = 0
    resolved: int = 0
    missing: list[str] = field(default_factory=list)


def generate_build_config(
    distro: str,
    ref: str,
    date: str,
    pinning_commit: str | None = None,
    repo_root: Path = REPO_ROOT,
) -> BuildConfigSummary:
    print(f"Fetching distribution.yaml for {distro} @ {ref}")
    dist_yaml = fetch_distribution_yaml(distro, ref)
    snapshot = parse_distribution(distro, dist_yaml, ref=ref)
    print(f"  {len(snapshot.packages)} packages in snapshot")

    print("Fetching conda-forge-pinning")
    cf_raw, cf_pins, cf_commit = fetch_pinning(pinning_commit)
    print(f"  commit {cf_commit[:10]} ({len(cf_pins)} keys)")

    overrides_path = repo_root / "conda_build_config.yaml"
    overrides = load_local_overrides(overrides_path)
    if overrides:
        print(f"  {len(overrides)} local overrides")

    scanned = scan_recipe_deps(repo_root, distro, snapshot)
    already_pinned = sum(
        1 for name in scanned if name in cf_pins or name in overrides
    )
    print(
        f"Scanned recipes: {len(scanned)} unique non-ROS deps"
        f" ({already_pinned} already pinned)"
    )

    to_resolve = len(scanned) - already_pinned
    if to_resolve:
        print(f"Fetching conda-forge channeldata ({to_resolve} deps to resolve)")
        channeldata = fetch_channeldata()
        latest = latest_versions(channeldata)
    else:
        latest = {}

    resolved, missing = resolve_unpinned(scanned, cf_pins, overrides, latest)
    print(f"  {len(resolved)} resolved, {len(missing)} missing on conda-forge")

    output_dir = repo_root / "distros" / distro / "snapshots" / date
    output_paths = generate_snapshot_build_config(
        snapshot=snapshot,
        conda_forge_pinning_raw=cf_raw,
        conda_forge_pinning_commit=cf_commit,
        local_overrides=overrides,
        resolved_deps=resolved,
        output_dir=output_dir,
    )
    for p in output_paths:
        print(f"Wrote {p.relative_to(repo_root)}")

    return BuildConfigSummary(
        output_paths=output_paths,
        pinning_commit=cf_commit,
        scanned=len(scanned),
        already_pinned=already_pinned,
        resolved=len(resolved),
        missing=missing,
    )


def print_summary(summary: BuildConfigSummary) -> None:
    print()
    print(f"conda-forge-pinning: {summary.pinning_commit}")
    print(f"Scanned deps:   {summary.scanned}")
    print(f"Already pinned: {summary.already_pinned}")
    print(f"Resolved:       {summary.resolved}")
    if summary.missing:
        print(f"Missing on conda-forge ({len(summary.missing)}):")
        for name in summary.missing:
            print(f"    {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--distro", required=True, help="ROS distro (e.g. jazzy)")
    parser.add_argument(
        "--ref",
        required=True,
        help="rosdistro git ref (tag or branch, e.g. jazzy/2026-04-13)",
    )
    parser.add_argument(
        "--date",
        required=True,
        help="snapshot date used for the output directory (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--pinning-commit",
        default=None,
        help="conda-forge-pinning commit to use (default: current main)",
    )
    args = parser.parse_args(argv)

    summary = generate_build_config(
        distro=args.distro,
        ref=args.ref,
        date=args.date,
        pinning_commit=args.pinning_commit,
    )
    print_summary(summary)

    return 1 if summary.missing else 0


if __name__ == "__main__":
    sys.exit(main())
