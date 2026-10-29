"""Check GitHub Releases for a newer FixMath version.

No third-party dependency; uses the public GitHub API.  The repository is
configured in ``version.py`` (``GITHUB_REPO``), so publishing a new release
only requires a new git tag + GitHub Release.
"""

from __future__ import annotations

import json
import re
import urllib.request

from version import GITHUB_REPO, __version__


def _parts(version: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", version)
    return tuple(int(n) for n in nums[:3]) or (0,)


def is_newer(latest: str, current: str = __version__) -> bool:
    return _parts(latest) > _parts(current)


def check_for_update(repo: str | None = None, timeout: float = 6.0) -> dict | None:
    """Return release info, or None if the check failed."""
    repo = (repo or GITHUB_REPO).strip()
    if not repo or "/" not in repo or repo.startswith("your-"):
        return None
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "FixMath-UpdateChecker",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - offline is the normal case
        return None

    tag = str(data.get("tag_name") or data.get("name") or "").strip()
    latest = tag.lstrip("vV")
    if not latest:
        return None
    assets = [
        {
            "name": item.get("name", ""),
            "url": item.get("browser_download_url", ""),
            "size": item.get("size", 0),
        }
        for item in data.get("assets", [])
    ]
    return {
        "current": __version__,
        "latest": latest,
        "newer": is_newer(latest),
        "name": data.get("name") or tag,
        "url": data.get("html_url", ""),
        "notes": data.get("body") or "",
        "assets": assets,
    }
