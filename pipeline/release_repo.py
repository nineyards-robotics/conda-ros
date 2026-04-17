"""Fetch package.xml from a bloom release repo at a release tag.

Bloom's gbp release tags point at a per-package flat tree, so
``package.xml`` is always at the root of the tagged tree regardless
of whether the upstream source repo is multi-package.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from urllib.parse import urlparse

from .rosdistro import PackageRelease


class ReleaseFetchError(RuntimeError):
    """Raised when a release repo fetch fails."""


def _raw_url(release_url: str, tag: str, path: str) -> str:
    """Build a raw.githubusercontent.com URL for *path* at *tag*.

    Only GitHub is supported — rosdistro release repos are hosted
    there in practice (ros2-gbp/*).
    """
    parsed = urlparse(release_url)
    if parsed.netloc != "github.com":
        raise ReleaseFetchError(
            f"unsupported release host {parsed.netloc!r}; only github.com handled"
        )
    repo_path = parsed.path
    if repo_path.endswith(".git"):
        repo_path = repo_path[: -len(".git")]
    repo_path = repo_path.strip("/")
    return f"https://raw.githubusercontent.com/{repo_path}/{tag}/{path}"


def package_xml_url(release: PackageRelease) -> str:
    """Return the raw URL for *release*'s package.xml."""
    return _raw_url(release.release_url, release.formatted_tag(), "package.xml")


def fetch_package_xml(release: PackageRelease, timeout: float = 30.0) -> bytes:
    """Fetch raw bytes of *release*'s package.xml.

    Raises ReleaseFetchError on any HTTP or network failure.
    """
    url = package_xml_url(release)
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise ReleaseFetchError(
            f"HTTP {e.code} fetching {url}"
        ) from e
    except urllib.error.URLError as e:
        raise ReleaseFetchError(f"network error fetching {url}: {e}") from e
