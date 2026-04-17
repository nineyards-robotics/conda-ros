"""CLI for Step 6: generate (and optionally build) the snapshot
constraint metapackage.

For each --target-platform, fetches `repodata.json` from every channel
(local output dir + any --channel flags), looks up the build for every
package in the snapshot's `distribution.yaml`, and writes a per-platform
`recipe.yaml` that pins them via `run_constrained`.

Pass `--build` to invoke rattler-build immediately after generation.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .rosdistro import fetch_distribution_yaml, parse_distribution
from .snapshot_metapackage import (
    NOARCH,
    build_recipe,
    collect_builds,
    metapackage_name,
    resolve_constraints,
    write_recipe,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _stage_dir(stage_root: Path, distro: str, date: str, platform: str) -> Path:
    return stage_root / f"snapshot-metapackage-{distro}-{date}-{platform}"


def _run_rattler(
    recipe_path: Path,
    output_dir: Path,
    channels: list[str],
    target_platform: str,
    rattler_build: str = "rattler-build",
) -> int:
    cmd = [
        rattler_build, "build",
        "--recipe", str(recipe_path),
        "--output-dir", str(output_dir),
        "--target-platform", target_platform,
        "--skip-existing", "all",
        "--log-style", "plain",
    ]
    for ch in channels:
        cmd.extend(["--channel", ch])
    proc = subprocess.run(cmd)
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--distro", required=True, help="ROS distro (e.g. jazzy)")
    parser.add_argument(
        "--ref",
        required=True,
        help="rosdistro git ref (e.g. jazzy/2026-04-13)",
    )
    parser.add_argument(
        "--date", required=True, help="snapshot date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--target-platform",
        action="append",
        required=True,
        help="target platform (e.g. linux-64). Repeat for multiple.",
    )
    parser.add_argument(
        "--channel",
        action="append",
        default=[],
        help=(
            "channel to scan for builds (URL or local path)."
            " Repeat to add more. The local --output-dir is added"
            " automatically as a file:// channel."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "build" / "output",
        help="local output directory containing built .conda artifacts",
    )
    parser.add_argument(
        "--stage-dir",
        type=Path,
        default=REPO_ROOT / "build" / "stage",
        help="staging dir where per-platform metapackage recipes are written",
    )
    parser.add_argument(
        "--build-number",
        type=int,
        default=0,
        help="build number for the metapackage recipe",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="invoke rattler-build to build each generated metapackage",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help=(
            "proceed even if some snapshot packages are missing from"
            " the channels (default: fail)"
        ),
    )
    args = parser.parse_args(argv)

    print(f"Fetching distribution.yaml for {args.distro} @ {args.ref}")
    dist_yaml = fetch_distribution_yaml(args.distro, args.ref)
    snapshot = parse_distribution(args.distro, dist_yaml, ref=args.ref)
    print(f"  {len(snapshot.packages)} packages in snapshot")

    channels: list[str] = list(args.channel)
    channels.append(args.output_dir.resolve().as_uri())

    subdirs_needed = sorted({*args.target_platform, NOARCH})
    print(f"Scanning channels for {subdirs_needed}:")
    for ch in channels:
        print(f"  {ch}")
    builds = collect_builds(channels, subdirs_needed)
    print(f"  found {len(builds)} build records")

    exit_code = 0
    for platform in args.target_platform:
        resolved = resolve_constraints(snapshot, builds, platform)
        print()
        print(f"[{platform}] pinned {len(resolved.constraints)} packages")
        if resolved.missing:
            print(f"[{platform}] missing {len(resolved.missing)}:")
            for cname, version in resolved.missing[:20]:
                print(f"    {cname} =={version}")
            if len(resolved.missing) > 20:
                print(f"    ... and {len(resolved.missing) - 20} more")
            if not args.allow_missing:
                exit_code = 1
                continue

        recipe = build_recipe(
            args.distro, args.date, resolved, build_number=args.build_number
        )
        stage_dir = _stage_dir(args.stage_dir, args.distro, args.date, platform)
        recipe_path = write_recipe(recipe, stage_dir)
        print(f"[{platform}] wrote {recipe_path}")

        if args.build:
            rc = _run_rattler(
                recipe_path=recipe_path,
                output_dir=args.output_dir,
                channels=[*channels, "conda-forge"],
                target_platform=platform,
            )
            if rc != 0:
                print(
                    f"[{platform}] rattler-build failed (rc={rc}) for "
                    f"{metapackage_name(args.distro, args.date)}",
                    file=sys.stderr,
                )
                exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
