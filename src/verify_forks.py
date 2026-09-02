from __future__ import annotations

import argparse
import csv
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .github_client import GitHubClient, GitHubError
from .pipeline import load_repositories


LOG = logging.getLogger(__name__)
FIELDS = [
    "original_repository", "fork_repository", "fork_exists", "is_expected_fork",
    "original_default_branch", "original_head_sha", "fork_default_branch",
    "fork_head_sha", "heads_match", "timestamp", "error",
]


def verify(client: GitHubClient, repositories: list[str], username: str) -> list[dict[str, str | bool]]:
    records = []
    for original_name in repositories:
        fork_name = f"{username}/{original_name.split('/', 1)[1]}"
        timestamp = datetime.now(timezone.utc).isoformat()
        record: dict[str, str | bool] = {
            "original_repository": original_name, "fork_repository": fork_name,
            "fork_exists": False, "is_expected_fork": False,
            "original_default_branch": "", "original_head_sha": "",
            "fork_default_branch": "", "fork_head_sha": "", "heads_match": False,
            "timestamp": timestamp, "error": "",
        }
        try:
            original_branch, original_head = remote_default_head(original_name)
            record["original_default_branch"] = original_branch
            record["original_head_sha"] = original_head
            fork = client.get(f"/repos/{fork_name}")
            record["fork_exists"] = True
            parent_name = ((fork.get("parent") or {}).get("full_name") or "").casefold()
            source_name = ((fork.get("source") or {}).get("full_name") or "").casefold()
            expected = original_name.casefold()
            record["is_expected_fork"] = bool(fork.get("fork")) and expected in {parent_name, source_name}
            fork_branch, fork_head = remote_default_head(fork_name)
            record["fork_default_branch"] = fork_branch
            record["fork_head_sha"] = fork_head
            record["heads_match"] = bool(record["original_head_sha"]) and record["original_head_sha"] == record["fork_head_sha"]
        except (GitHubError, subprocess.SubprocessError) as exc:
            record["error"] = str(exc)
            LOG.error("Verification failed for %s -> %s: %s", original_name, fork_name, exc)
        records.append(record)
    return records


def remote_default_head(repository: str) -> tuple[str, str]:
    result = subprocess.run(
        ["git", "ls-remote", "--symref", f"https://github.com/{repository}.git", "HEAD"],
        check=True, text=True, encoding="utf-8", errors="replace", capture_output=True,
    )
    line = result.stdout.strip().splitlines()
    if not line:
        raise GitHubError(f"Default branch HEAD not found for {repository}")
    ref_line = next((item for item in line if item.startswith("ref: refs/heads/")), "")
    sha_line = next((item for item in line if not item.startswith("ref:")), "")
    if not ref_line or not sha_line:
        raise GitHubError(f"Could not resolve default branch for {repository}")
    branch = ref_line.split("refs/heads/", 1)[1].split()[0]
    return branch, sha_line.split()[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only verification of standard GitHub fork mappings")
    parser.add_argument("--repos", default="repos.txt")
    parser.add_argument("--username", required=True)
    parser.add_argument("--output", default="data/repositories.csv")
    parser.add_argument("--retries", type=int, default=5)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    records = verify(GitHubClient(retries=args.retries), load_repositories(Path(args.repos)), args.username)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(records)
    failures = [record for record in records if not record["fork_exists"] or not record["is_expected_fork"] or record["error"]]
    LOG.info("Verified %d mappings; %d missing/invalid/error", len(records), len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
