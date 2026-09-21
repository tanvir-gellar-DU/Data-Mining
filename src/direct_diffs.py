"""Public GitHub diff downloads, preserving the requested comparison endpoints."""
import logging
from pathlib import Path
from urllib.parse import quote

from .github_client import GitHubClient, GitHubError
from .git_comparison import shallow_diff

LOG = logging.getLogger(__name__)


def download_diff(client: GitHubClient, repository: str, base: str, head: str,
                  cache: Path, local_fallback: bool = True,
                  single_parent_commit: bool = False,
                  allow_empty: bool = False) -> str:
    if base == head:
        return ""
    repo = quote(repository, safe="/")
    # Use commit URLs only after metadata proves the requested base is its sole parent.
    # Merge commits and episode pairs use two-dot endpoint comparisons, never PR diffs.
    suffix = (f"commit/{quote(head, safe='')}.diff" if single_parent_commit else
              f"compare/{quote(base, safe='')}..{quote(head, safe='')}.diff")
    url = f"https://github.com/{repo}/{suffix}"
    try:
        diff = client.get_public_diff(url)
        # GitHub returns an empty body for a valid empty commit diff. Accept it
        # only when the paginated commit metadata independently reported no files.
        if not diff and allow_empty:
            LOG.info("Downloaded empty public diff for %s %s..%s", repository, base, head)
            return ""
        if not diff.startswith("diff --git "):
            raise GitHubError(f"Unverified empty or malformed diff from {url}")
        LOG.info("Downloaded public diff for %s %s..%s", repository, base, head)
        return diff
    except GitHubError as exc:
        if not local_fallback:
            raise
        LOG.warning("Direct diff unavailable; using Git for %s %s..%s: %s", repository, base, head, exc)
        return shallow_diff(repository, base, head, cache)
