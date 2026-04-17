"""Orchestrator for Step 4: resolve and print the snapshot build order.

Scans all recipe.yaml files for the snapshot's packages, builds a
ROS-on-ROS dependency graph, and emits a topologically sorted order.
Missing deps and cycles are surfaced in the summary.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .build_order import BuildOrder, resolve_build_order
from .rosdistro import fetch_distribution_yaml, parse_distribution

REPO_ROOT = Path(__file__).resolve().parent.parent


def print_summary(result: BuildOrder, verbose: bool) -> None:
    print()
    print(f"Ordered {len(result.order)} packages")
    if verbose:
        for i, node in enumerate(result.order, 1):
            print(f"  {i:4d}. {node.conda_name} {node.version}")
    if result.missing_deps:
        total = sum(len(v) for v in result.missing_deps.values())
        print(
            f"Missing ROS deps ({total} edges across"
            f" {len(result.missing_deps)} packages):"
        )
        for pkg in sorted(result.missing_deps):
            for dep in sorted(result.missing_deps[pkg]):
                print(f"    {pkg} -> {dep}")
    if result.cycle_nodes:
        print(f"Cycle blocks {len(result.cycle_nodes)} packages:")
        for name in result.cycle_nodes:
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
        "--verbose",
        "-v",
        action="store_true",
        help="print the full build order",
    )
    args = parser.parse_args(argv)

    print(f"Fetching distribution.yaml for {args.distro} @ {args.ref}")
    dist_yaml = fetch_distribution_yaml(args.distro, args.ref)
    snapshot = parse_distribution(args.distro, dist_yaml, ref=args.ref)
    print(f"  {len(snapshot.packages)} packages in snapshot")

    result = resolve_build_order(REPO_ROOT, args.distro, snapshot)
    print_summary(result, args.verbose)

    return 1 if result.cycle_nodes else 0


if __name__ == "__main__":
    sys.exit(main())
