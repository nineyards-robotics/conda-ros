"""Orchestrator for Step 5: build a snapshot's packages in dep order.

Fetches the snapshot metadata, resolves the build order from the
on-disk recipes, and invokes rattler-build for each package using the
snapshot's conda_build_config.yaml.  A local output directory is always
added to the channel list so later packages can resolve against earlier
builds before they're pushed remotely.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .build import BuildSummary, run_build
from .build_config import snapshot_config_paths
from .build_order import resolve_build_order
from .rosdistro import fetch_distribution_yaml, parse_distribution

REPO_ROOT = Path(__file__).resolve().parent.parent


def _snapshot_config_paths(repo_root: Path, distro: str, date: str) -> list[Path]:
    """Return the variant-config paths for a snapshot in rattler-build order."""
    output_dir = repo_root / "distros" / distro / "snapshots" / date
    return snapshot_config_paths(output_dir)


def print_summary(summary: BuildSummary) -> None:
    print()
    print(f"Built   {len(summary.built)}")
    print(f"Skipped {len(summary.skipped)}")
    print(f"Failed  {len(summary.failed)}")
    if summary.failed:
        print()
        print("Failures:")
        for r in summary.failed:
            print(f"  {r.node.conda_name} {r.node.version}")
            if r.log_tail:
                for line in r.log_tail.splitlines()[-10:]:
                    print(f"      {line}")


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
        help="snapshot date (YYYY-MM-DD) — selects the conda_build_config.yaml",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "build" / "output",
        help="local output directory for built .conda artifacts",
    )
    parser.add_argument(
        "--stage-dir",
        type=Path,
        default=REPO_ROOT / "build" / "stage",
        help="staging directory for recipes + patches (temporary)",
    )
    parser.add_argument(
        "--channel",
        action="append",
        default=[],
        help=(
            "channel to use for dep resolution and --skip-existing lookup."
            " Repeat to add more; conda-forge is always added last."
        ),
    )
    parser.add_argument(
        "--target-platform",
        default=None,
        help="target platform (e.g. linux-64). Defaults to native.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="only build the first N packages in order (for testing)",
    )
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="abort the run on the first failure instead of continuing",
    )
    parser.add_argument(
        "--test",
        default=None,
        choices=["skip", "native", "native-and-emulated"],
        help="pass through to rattler-build --test",
    )
    args = parser.parse_args(argv)

    variant_configs = _snapshot_config_paths(REPO_ROOT, args.distro, args.date)
    missing = [p for p in variant_configs if not p.exists()]
    if missing:
        for p in missing:
            print(f"Missing snapshot config: {p}", file=sys.stderr)
        print(
            "Run `pixi run generate-build-config` first.", file=sys.stderr
        )
        return 2

    print(f"Fetching distribution.yaml for {args.distro} @ {args.ref}")
    dist_yaml = fetch_distribution_yaml(args.distro, args.ref)
    snapshot = parse_distribution(args.distro, dist_yaml, ref=args.ref)
    print(f"  {len(snapshot.packages)} packages in snapshot")

    order_result = resolve_build_order(REPO_ROOT, args.distro, snapshot)
    if order_result.cycle_nodes:
        print(
            f"Cycle blocks {len(order_result.cycle_nodes)} packages — aborting",
            file=sys.stderr,
        )
        return 2
    order = order_result.order
    if args.limit is not None:
        order = order[: args.limit]
    print(f"  building {len(order)} packages")

    # Always include the local output dir as a channel so later builds
    # resolve against earlier ones.  User-specified channels come first
    # (so the remote channel controls skip-existing), conda-forge last.
    channels: list[str] = list(args.channel)
    channels.append(args.output_dir.resolve().as_uri())
    channels.append("conda-forge")

    extra_args: list[str] = []
    if args.test:
        extra_args.extend(["--test", args.test])

    summary = run_build(
        order=order,
        variant_configs=variant_configs,
        channels=channels,
        output_dir=args.output_dir,
        stage_root=args.stage_dir,
        target_platform=args.target_platform,
        extra_args=extra_args,
        stop_on_failure=args.stop_on_failure,
    )
    print_summary(summary)

    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
