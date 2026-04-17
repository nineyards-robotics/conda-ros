"""Fetch conda-forge channeldata.json for latest-version lookups.

Used by Step 3 to resolve dependency names in recipes that aren't
already pinned by conda-forge-pinning or local overrides.
"""

from __future__ import annotations

import json
import urllib.request

_URL = "https://conda.anaconda.org/conda-forge/channeldata.json"


def fetch_channeldata(timeout: float = 120.0) -> dict:
    """Fetch the conda-forge channeldata.json blob."""
    req = urllib.request.Request(_URL)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def latest_versions(channeldata: dict) -> dict[str, str]:
    """Map package name to latest version string."""
    packages = channeldata.get("packages") or {}
    out: dict[str, str] = {}
    for name, info in packages.items():
        version = info.get("version") if isinstance(info, dict) else None
        if version:
            out[name] = version
    return out
