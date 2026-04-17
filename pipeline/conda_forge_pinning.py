"""Fetch conda-forge-pinning system pins.

Retrieves conda_build_config.yaml from the conda-forge-pinning-feedstock
at a specific commit (or main) to get ecosystem-wide dependency pins.
"""

from __future__ import annotations

import json
import urllib.request

import yaml

_REPO = "conda-forge/conda-forge-pinning-feedstock"
_RAW_URL = f"https://raw.githubusercontent.com/{_REPO}"
_API_URL = f"https://api.github.com/repos/{_REPO}"
_CONFIG_PATH = "recipe/conda_build_config.yaml"


def get_main_commit() -> str:
    """Get the current commit hash of main."""
    req = urllib.request.Request(
        f"{_API_URL}/commits/main",
        headers={"Accept": "application/vnd.github.sha"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode().strip()


def fetch_pinning(commit: str | None = None) -> tuple[str, dict, str]:
    """Fetch conda-forge-pinning config.

    If *commit* is None, resolves main to its current commit first.

    Returns ``(raw_text, parsed_config, commit_hash)``.  Both raw and
    parsed forms are needed: rattler-build consumes the raw text so the
    ``# [selector]`` comments drive per-platform subsetting, while the
    parsed dict is used for key-membership checks during dep resolution.
    """
    if commit is None:
        commit = get_main_commit()

    url = f"{_RAW_URL}/{commit}/{_CONFIG_PATH}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()

    config = yaml.safe_load(raw) or {}
    return raw, config, commit
