"""Step 5: build packages in dependency order.

Invokes rattler-build for each package in the order from Step 4.  Uses
``--skip-existing=all`` against the configured channels so unchanged
packages are skipped by content-addressed hash.  Patches in
``packages/{pkg}/{version}/patches/`` are applied by staging a temp
recipe that references them via ``source.patches``.

Failures don't abort the run — we keep going on the rest (best-effort,
largest working subset).  The summary counts built / skipped / failed.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from .build_order import BuildNode

BuildStatus = Literal["built", "skipped", "failed"]


@dataclass
class BuildResult:
    node: BuildNode
    status: BuildStatus
    duration_s: float
    log_tail: str = ""


@dataclass
class BuildSummary:
    results: list[BuildResult] = field(default_factory=list)

    @property
    def built(self) -> list[BuildResult]:
        return [r for r in self.results if r.status == "built"]

    @property
    def skipped(self) -> list[BuildResult]:
        return [r for r in self.results if r.status == "skipped"]

    @property
    def failed(self) -> list[BuildResult]:
        return [r for r in self.results if r.status == "failed"]


def _list_patches(recipe_path: Path) -> list[Path]:
    patches_dir = recipe_path.parent / "patches"
    if not patches_dir.is_dir():
        return []
    return sorted(patches_dir.glob("*.patch"))


def stage_recipe(node: BuildNode, stage_root: Path) -> Path:
    """Stage a recipe + patches under ``stage_root`` and return its dir.

    If patches exist next to the recipe, they are copied into a
    ``patches/`` subdirectory of the stage dir and referenced from the
    recipe's ``source.patches`` field with relative paths.  Recipes
    without patches are copied verbatim — staging still isolates the
    build from the source tree.
    """
    stage_dir = stage_root / f"{node.conda_name}-{node.version}"
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True)

    recipe = yaml.safe_load(node.recipe_path.read_text()) or {}
    patches = _list_patches(node.recipe_path)
    if patches:
        dest_dir = stage_dir / "patches"
        dest_dir.mkdir()
        rel_paths: list[str] = []
        for p in patches:
            shutil.copy2(p, dest_dir / p.name)
            rel_paths.append(f"patches/{p.name}")
        source = recipe.setdefault("source", {})
        existing = list(source.get("patches") or [])
        source["patches"] = existing + rel_paths

    staged = stage_dir / "recipe.yaml"
    with staged.open("w") as f:
        yaml.dump(recipe, f, sort_keys=False, default_flow_style=False)
    return stage_dir


def _snapshot_outputs(output_dir: Path) -> set[Path]:
    if not output_dir.exists():
        return set()
    return set(output_dir.rglob("*.conda")) | set(output_dir.rglob("*.tar.bz2"))


def build_package(
    node: BuildNode,
    variant_configs: list[Path],
    channels: list[str],
    output_dir: Path,
    stage_root: Path,
    target_platform: str | None = None,
    rattler_build: str = "rattler-build",
    extra_args: list[str] | None = None,
) -> BuildResult:
    """Run rattler-build for a single package.

    ``variant_configs`` are passed to rattler-build as repeated ``-m``
    flags in order — later files override earlier ones on duplicate
    keys.  See ``pipeline.build_config`` for the snapshot's layering.

    Returns a BuildResult — status is determined by whether a new
    artifact appeared in ``output_dir`` (built) or not (skipped).  A
    non-zero rattler-build exit is always ``failed``.
    """
    start = time.monotonic()
    stage_dir = stage_recipe(node, stage_root)
    before = _snapshot_outputs(output_dir)

    cmd: list[str] = [
        rattler_build, "build",
        "--recipe", str(stage_dir / "recipe.yaml"),
        "--output-dir", str(output_dir),
        "--skip-existing", "all",
        "--log-style", "plain",
    ]
    for vc in variant_configs:
        cmd.extend(["--variant-config", str(vc)])
    for ch in channels:
        cmd.extend(["--channel", ch])
    if target_platform:
        cmd.extend(["--target-platform", target_platform])
    if extra_args:
        cmd.extend(extra_args)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        return BuildResult(
            node=node,
            status="failed",
            duration_s=time.monotonic() - start,
            log_tail=f"rattler-build not found: {e}",
        )

    duration = time.monotonic() - start

    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr)[-4000:]
        return BuildResult(
            node=node, status="failed", duration_s=duration, log_tail=tail
        )

    new_files = _snapshot_outputs(output_dir) - before
    status: BuildStatus = "built" if new_files else "skipped"
    return BuildResult(node=node, status=status, duration_s=duration)


def run_build(
    order: list[BuildNode],
    variant_configs: list[Path],
    channels: list[str],
    output_dir: Path,
    stage_root: Path,
    target_platform: str | None = None,
    rattler_build: str = "rattler-build",
    extra_args: list[str] | None = None,
    progress: bool = True,
    stop_on_failure: bool = False,
) -> BuildSummary:
    """Run every package in ``order`` through rattler-build sequentially.

    Local artifacts accumulate in ``output_dir`` — add it to ``channels``
    (via ``file://`` URL) so downstream packages can resolve against the
    local builds when the remote channel hasn't been published yet.
    """
    summary = BuildSummary()
    total = len(order)
    output_dir.mkdir(parents=True, exist_ok=True)
    stage_root.mkdir(parents=True, exist_ok=True)

    for i, node in enumerate(order, 1):
        if progress:
            print(
                f"[{i}/{total}] {node.conda_name} {node.version}",
                flush=True,
            )
        result = build_package(
            node=node,
            variant_configs=variant_configs,
            channels=channels,
            output_dir=output_dir,
            stage_root=stage_root,
            target_platform=target_platform,
            rattler_build=rattler_build,
            extra_args=extra_args,
        )
        summary.results.append(result)
        if progress:
            print(
                f"    -> {result.status} ({result.duration_s:.1f}s)",
                flush=True,
            )
        if stop_on_failure and result.status == "failed":
            break

    return summary
