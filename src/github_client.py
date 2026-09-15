from __future__ import annotations

import json
import http.client
import logging
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator


LOG = logging.getLogger(__name__)


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str | None = None, retries: int = 5, timeout: int = 60):
        self.token = token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
        self.retries = retries
        self.timeout = timeout
        self.base_url = "https://api.github.com"

    def _request(self, path: str, accept: str = "application/vnd.github+json") -> tuple[bytes, dict[str, str]]:
        url = path if path.startswith("http") else self.base_url + path
        headers = {
            "Accept": accept,
            "User-Agent": "github-ci-episode-miner/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=self.timeout) as response:
                    return response.read(), dict(response.headers.items())
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                remaining = exc.headers.get("X-RateLimit-Remaining")
                reset = exc.headers.get("X-RateLimit-Reset")
                retry_after = exc.headers.get("Retry-After")
                transient = exc.code in (429, 500, 502, 503, 504) or (exc.code == 403 and remaining == "0")
                if not transient or attempt == self.retries:
                    raise GitHubError(f"GitHub API {exc.code} for {url}: {body[:500]}") from exc
                if retry_after:
                    wait = float(retry_after)
                elif remaining == "0" and reset:
                    wait = max(1.0, int(reset) - time.time() + 1)
                else:
                    wait = min(60.0, 2**attempt + random.random())
                LOG.warning("GitHub request failed (%s); retrying in %.1fs", exc.code, wait)
                time.sleep(wait)
            except (urllib.error.URLError, TimeoutError, http.client.IncompleteRead, ConnectionError) as exc:
                if attempt == self.retries:
                    raise GitHubError(f"GitHub request failed for {url}: {exc}") from exc
                wait = min(60.0, 2**attempt + random.random())
                LOG.warning("Network error; retrying in %.1fs: %s", wait, exc)
                time.sleep(wait)
        raise AssertionError("unreachable")

    def get(self, path: str) -> Any:
        body, _ = self._request(path)
        return json.loads(body)

    def get_text(self, path: str, accept: str) -> str:
        body, _ = self._request(path, accept)
        return body.decode("utf-8", "replace")

    def paginate(self, path: str, item_key: str | None = None) -> Iterator[dict[str, Any]]:
        separator = "&" if "?" in path else "?"
        page = 1
        while True:
            payload = self.get(f"{path}{separator}per_page=100&page={page}")
            items = payload.get(item_key, []) if item_key else payload
            if not isinstance(items, list):
                raise GitHubError(f"Expected a list while paginating {path}")
            yield from items
            if len(items) < 100:
                break
            page += 1

    @staticmethod
    def quote_sha(sha: str) -> str:
        return urllib.parse.quote(sha, safe="")
