"""Step 4: resolve build order from generated recipes.

Builds a ROS-on-ROS dependency graph from the recipe.yaml files for a
snapshot and topologically sorts it so every package's deps are built
before it.  Non-ROS deps come from conda-forge and don't affect order;
only edges between packages we're building matter here.

Test requirements are included as graph edges — rattler-build installs
them alongside build deps when running tests, so they need to be in the
channel first.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .build_config import conda_package_name
from .recipe import recipe_path
from .rosdistro import DistroSnapshot


@dataclass
class BuildNode:
    ros_name: str
    conda_name: str
    version: str
    recipe_path: Path


@dataclass
class BuildOrder:
    order: list[BuildNode] = field(default_factory=list)
    # Packages referenced as ROS deps that don't have a recipe in the
    # snapshot — keyed by depender.  Not a hard error: they may be
    # provided by an older snapshot already in the channel, or indicate
    # a recipe generation gap worth investigating.
    missing_deps: dict[str, set[str]] = field(default_factory=dict)
    # Packages left unordered because of a cycle.  Empty on success.
    cycle_nodes: list[str] = field(default_factory=list)


def _collect_names(entry: Any, out: set[str]) -> None:
    """Extract bare package names from a requirements entry.

    Mirrors build_config._collect_names — kept local to avoid widening
    that module's public surface for an internal helper.
    """
    if isinstance(entry, str):
        if entry.startswith("$"):
            return
        token = entry.split()[0] if entry.strip() else ""
        if token:
            out.add(token)
    elif isinstance(entry, dict):
        for sub in entry.get("then") or []:
            _collect_names(sub, out)
        for sub in entry.get("else") or []:
            _collect_names(sub, out)


def _recipe_deps(recipe: dict) -> set[str]:
    """All dep names referenced by a recipe across build/host/run/tests."""
    names: set[str] = set()
    reqs = recipe.get("requirements") or {}
    for section in ("build", "host", "run"):
        for entry in reqs.get(section) or []:
            _collect_names(entry, names)
    for test in recipe.get("tests") or []:
        test_reqs = test.get("requirements") or {}
        for entry in test_reqs.get("run") or []:
            _collect_names(entry, names)
    return names


def load_recipes(
    repo_root: Path,
    distro: str,
    snapshot: DistroSnapshot,
) -> dict[str, BuildNode]:
    """Load every snapshot package that has a recipe on disk.

    Returns a map from conda package name to BuildNode.  Packages
    without a recipe are skipped silently — Step 2 already reports
    those.
    """
    nodes: dict[str, BuildNode] = {}
    for release in snapshot.packages.values():
        path = recipe_path(repo_root, distro, release.name, release.version)
        if not path.exists():
            continue
        conda_name = conda_package_name(distro, release.name)
        nodes[conda_name] = BuildNode(
            ros_name=release.name,
            conda_name=conda_name,
            version=release.version,
            recipe_path=path,
        )
    return nodes


def build_graph(
    nodes: dict[str, BuildNode],
    distro: str,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Build an adjacency list of ROS-on-ROS deps.

    ``graph[pkg]`` is the set of in-graph packages ``pkg`` depends on.
    ``missing[pkg]`` is the set of ROS deps referenced by ``pkg`` that
    don't have a recipe in the current set.
    """
    ros_prefix = f"ros-{distro}-"
    graph: dict[str, set[str]] = {name: set() for name in nodes}
    missing: dict[str, set[str]] = defaultdict(set)
    for name, node in nodes.items():
        data = yaml.safe_load(node.recipe_path.read_text()) or {}
        for dep in _recipe_deps(data):
            if not dep.startswith(ros_prefix):
                continue
            if dep == name:
                continue
            if dep in nodes:
                graph[name].add(dep)
            else:
                missing[name].add(dep)
    return graph, dict(missing)


def topological_sort(
    graph: dict[str, set[str]],
) -> tuple[list[str], list[str]]:
    """Kahn's algorithm with alphabetical tie-breaks.

    Returns ``(order, leftover)``.  ``leftover`` is non-empty only when a
    cycle prevents a full ordering.
    """
    in_degree = {u: len(deps) for u, deps in graph.items()}
    dependents: dict[str, set[str]] = {u: set() for u in graph}
    for u, deps in graph.items():
        for v in deps:
            dependents[v].add(u)

    ready = [u for u in graph if in_degree[u] == 0]
    heapq.heapify(ready)

    order: list[str] = []
    while ready:
        u = heapq.heappop(ready)
        order.append(u)
        for v in dependents[u]:
            in_degree[v] -= 1
            if in_degree[v] == 0:
                heapq.heappush(ready, v)

    leftover = sorted(u for u, d in in_degree.items() if d > 0)
    return order, leftover


def resolve_build_order(
    repo_root: Path,
    distro: str,
    snapshot: DistroSnapshot,
) -> BuildOrder:
    """Top-level entry point — see module docstring."""
    nodes = load_recipes(repo_root, distro, snapshot)
    graph, missing = build_graph(nodes, distro)
    order_names, leftover = topological_sort(graph)
    return BuildOrder(
        order=[nodes[n] for n in order_names],
        missing_deps=missing,
        cycle_nodes=leftover,
    )
