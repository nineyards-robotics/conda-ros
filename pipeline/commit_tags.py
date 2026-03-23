"""Generate structured commit tags from git diff.

Analyzes changed files and produces tags like:
    [recipe-add][jazzy][nav2][1.3.0]
    [patch-update][jazzy][nav2][1.2.0]
    [snapshot-add][jazzy][2026-03-15]

Usage:
    python -m pipeline.commit_tags          # diff against HEAD
    python -m pipeline.commit_tags --staged  # diff staged changes only
"""

import subprocess
import sys
from pathlib import PurePosixPath


def get_changed_files(staged: bool = False) -> list[tuple[str, str]]:
    """Return list of (status, path) from git diff."""
    cmd = ["git", "diff", "--name-status"]
    if staged:
        cmd.append("--cached")
    else:
        cmd.append("HEAD")
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    files = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            files.append((parts[0], parts[1]))
    return files


def classify(status: str, path: str) -> str | None:
    """Classify a changed file into a commit tag, or None if not taggable."""
    p = PurePosixPath(path)
    parts = p.parts

    # pipeline/
    if parts[0] == "pipeline":
        return "[pipeline-update]"

    # rosdep.yaml
    if path == "rosdep.yaml":
        return "[rosdep-update]"

    # conda_build_config.yaml (root only)
    if path == "conda_build_config.yaml":
        return "[build-config-update]"

    # distros/{distro}/...
    if parts[0] == "distros" and len(parts) >= 2:
        distro = parts[1]

        # distro.yaml
        if len(parts) == 3 and parts[2] == "distro.yaml":
            return f"[distro-config-update][{distro}]"

        # packages/{pkg}/{version}/recipe.yaml
        # packages/{pkg}/{version}/patches/...
        if len(parts) >= 5 and parts[2] == "packages":
            pkg = parts[3]
            version = parts[4]

            if len(parts) >= 6 and parts[5] == "patches":
                action = _action("patch", status)
                return f"[{action}][{distro}][{pkg}][{version}]"

            if len(parts) == 6 and parts[5] == "recipe.yaml":
                action = _action("recipe", status)
                return f"[{action}][{distro}][{pkg}][{version}]"

        # snapshots/{date}/...
        if len(parts) >= 4 and parts[2] == "snapshots":
            date = parts[3]
            action = _action("snapshot", status)
            return f"[{action}][{distro}][{date}]"

    return None


def _action(prefix: str, git_status: str) -> str:
    """Map git status to action verb."""
    if git_status == "A":
        return f"{prefix}-add"
    elif git_status == "D":
        return f"{prefix}-remove"
    else:
        return f"{prefix}-update"


def generate_tags(staged: bool = False) -> list[str]:
    """Generate deduplicated, sorted commit tags."""
    files = get_changed_files(staged)
    tags = set()
    for status, path in files:
        tag = classify(status, path)
        if tag:
            tags.add(tag)
    return sorted(tags)


if __name__ == "__main__":
    staged = "--staged" in sys.argv
    tags = generate_tags(staged)
    for tag in tags:
        print(tag)
