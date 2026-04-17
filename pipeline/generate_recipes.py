"""Orchestrator for Step 2: generate recipes for a snapshot.

Iterates the packages in a rosdistro snapshot, skips ones whose
recipe.yaml already exists, and for the remainder fetches package.xml
from the release repo, parses it, and writes a recipe.

Unknown rosdep keys and per-package failures are surfaced in a summary
at the end so the whole run isn't aborted by a handful of bad entries.
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .package_xml import parse_package_xml
from .recipe import RecipeResult, generate, recipe_path
from .release_repo import ReleaseFetchError, fetch_package_xml
from .rosdep_mapping import load_rosdep
from .rosdistro import (
    DistroSnapshot,
    PackageRelease,
    fetch_distribution_yaml,
    parse_distribution,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class RunSummary:
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    unknown_keys: dict[str, set[str]] = field(default_factory=dict)

    def record_unknown(self, package: str, keys: set[str]) -> None:
        if keys:
            self.unknown_keys[package] = keys


def load_vendor_passthrough(distro: str, repo_root: Path) -> set[str]:
    """Load vendor_passthrough list from distros/{distro}/distro.yaml."""
    path = repo_root / "distros" / distro / "distro.yaml"
    if not path.exists():
        return set()
    text = path.read_text()
    if not text.strip():
        return set()
    data = yaml.safe_load(text) or {}
    vendors = data.get("vendor_passthrough") or []
    return set(vendors)


def _process_one(
    release: PackageRelease,
    snapshot: DistroSnapshot,
    rosdep_map: dict,
    vendor_passthrough: set[str],
    repo_root: Path,
) -> RecipeResult:
    """Fetch, parse and (conditionally) write one recipe."""
    xml_bytes = fetch_package_xml(release)
    manifest = parse_package_xml(xml_bytes)
    return generate(
        release,
        manifest,
        snapshot,
        rosdep_map,
        vendor_passthrough,
        repo_root,
    )


def generate_recipes(
    distro: str,
    ref: str,
    repo_root: Path = REPO_ROOT,
    max_workers: int = 16,
) -> RunSummary:
    """Main entry point. Returns a RunSummary."""
    print(f"Fetching distribution.yaml for {distro} @ {ref}")
    dist_yaml = fetch_distribution_yaml(distro, ref)
    snapshot = parse_distribution(distro, dist_yaml, ref=ref)
    print(f"  {len(snapshot.packages)} packages in snapshot")

    rosdep_map = load_rosdep(repo_root / "rosdep.yaml")
    vendor_passthrough = load_vendor_passthrough(distro, repo_root)
    if vendor_passthrough:
        print(f"  {len(vendor_passthrough)} vendor passthrough packages")

    summary = RunSummary()
    pending: list[PackageRelease] = []
    for release in snapshot.packages.values():
        path = recipe_path(repo_root, distro, release.name, release.version)
        if path.exists():
            summary.skipped.append(release.name)
        else:
            pending.append(release)

    print(
        f"  {len(summary.skipped)} existing recipes skipped,"
        f" {len(pending)} to generate"
    )
    if not pending:
        return summary

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {
            pool.submit(
                _process_one,
                release,
                snapshot,
                rosdep_map,
                vendor_passthrough,
                repo_root,
            ): release
            for release in pending
        }
        for future in as_completed(future_map):
            release = future_map[future]
            try:
                result = future.result()
            except ReleaseFetchError as e:
                summary.failures[release.name] = f"fetch: {e}"
                continue
            except Exception as e:  # parse / yaml / IO
                summary.failures[release.name] = f"{type(e).__name__}: {e}"
                continue
            if result.path is None:
                summary.deferred.append(release.name)
            else:
                summary.written.append(release.name)
            summary.record_unknown(release.name, result.unknown_keys)

    return summary


def print_summary(summary: RunSummary) -> None:
    print()
    print(f"Wrote    {len(summary.written)} recipes")
    print(f"Skipped  {len(summary.skipped)} existing")
    print(f"Deferred {len(summary.deferred)} (unresolved rosdep keys)")
    if summary.failures:
        print(f"Failed   {len(summary.failures)}:")
        for name, reason in sorted(summary.failures.items()):
            print(f"    {name}: {reason}")
    if summary.unknown_keys:
        # Invert: key -> packages that referenced it. Easier to fix
        # because one rosdep.yaml edit unblocks many packages.
        by_key: dict[str, list[str]] = {}
        for pkg, keys in summary.unknown_keys.items():
            for key in keys:
                by_key.setdefault(key, []).append(pkg)
        print(f"Unknown rosdep keys ({len(by_key)}):")
        for key in sorted(by_key):
            pkgs = sorted(by_key[key])
            shown = ", ".join(pkgs[:5])
            extra = f" (+{len(pkgs) - 5} more)" if len(pkgs) > 5 else ""
            print(f"    {key}: {shown}{extra}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--distro", required=True, help="ROS distro (e.g. jazzy)")
    parser.add_argument(
        "--ref",
        required=True,
        help="rosdistro git ref (tag or branch, e.g. jazzy/2026-04-13)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=16,
        help="parallel fetch workers (default: 16)",
    )
    args = parser.parse_args(argv)

    summary = generate_recipes(
        distro=args.distro,
        ref=args.ref,
        max_workers=args.workers,
    )
    print_summary(summary)

    return 1 if summary.failures else 0


if __name__ == "__main__":
    sys.exit(main())
