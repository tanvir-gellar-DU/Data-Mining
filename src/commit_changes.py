from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from .github_client import GitHubClient, GitHubError
from .direct_diffs import download_diff
from .storage import read_jsonl, repo_key, write_jsonl


LOG = logging.getLogger(__name__)


def _run_refs(episodes: list[dict[str, Any]]) -> set[tuple[str, str]]:
    refs = set()
    for episode in episodes:
        for run in [*episode["failures"], episode["recovery_pass"]]:
            if run.get("head_sha"):
                refs.add((episode["repository"], run["head_sha"]))
    return refs


def enrich(client: GitHubClient, episodes: list[dict[str, Any]], output: Path, local_fallback: bool = True, preserve_other_repositories: bool = False) -> list[dict[str, str]]:
    selected = {e["repository"] for e in episodes}
    change_records = [r for r in read_jsonl(output / "commit_changes" / "commits.jsonl")
                      if preserve_other_repositories and r["repository"] not in selected]
    errors: list[dict[str, str]] = []
    for repository, sha in sorted(_run_refs(episodes)):
        try:
            data = client.get(f"/repos/{repository}/commits/{sha}?per_page=100&page=1")
            parent = (data.get("parents") or [{}])[0].get("sha")
            raw_files = list(data.get("files", []))
            page = 2
            while len(raw_files) == (page - 1) * 100:
                next_page = client.get(f"/repos/{repository}/commits/{sha}?per_page=100&page={page}").get("files", [])
                raw_files.extend(next_page)
                if len(next_page) < 100:
                    break
                page += 1
            files = [{key: item.get(key) for key in ("filename", "status", "additions", "deletions", "changes", "patch", "previous_filename")} for item in raw_files]
            record = {"repository": repository, "commit_sha": sha, "parent_sha": parent, "files": files}
            change_records.append(record)
            if parent:
                _save_diff(client, repository, parent, sha, "per_commit", output, local_fallback,
                           single_parent_commit=len(data.get("parents", [])) == 1,
                           allow_empty=not raw_files)
        except (GitHubError, subprocess.SubprocessError, OSError) as exc:
            LOG.exception("Commit enrichment failed for %s@%s", repository, sha)
            errors.append({"repository": repository, "stage": "commit_enrichment", "sha": sha, "error": str(exc)})
    write_jsonl(output / "commit_changes" / "commits.jsonl", change_records)

    comparisons = [r for r in read_jsonl(output / "diffs" / "comparisons.jsonl")
                   if preserve_other_repositories and r["repository"] not in selected]
    for episode in episodes:
        repository = episode["repository"]
        last_failed = episode["failures"][-1]["head_sha"]
        recovery = episode["recovery_pass"]["head_sha"]
        try:
            path = _save_diff(client, repository, last_failed, recovery, "repair", output, local_fallback)
            previous = episode["previous_pass"].get("head_sha")
            previous_path = _save_diff(client, repository, previous, recovery, "previous_pass_to_recovery", output, local_fallback) if previous else None
            comparisons.append({"episode_id": episode["episode_id"], "repository": repository, "base_sha": last_failed, "head_sha": recovery, "meaning": "last_failed_to_recovery", "diff_path": str(path), "previous_pass_diff_path": str(previous_path) if previous_path else None})
        except (GitHubError, subprocess.SubprocessError, OSError) as exc:
            LOG.exception("Comparison enrichment failed for episode %s", episode["episode_id"])
            errors.append({"repository": repository, "stage": "comparison_enrichment", "episode_id": episode["episode_id"], "error": str(exc)})
    write_jsonl(output / "diffs" / "comparisons.jsonl", comparisons)
    return errors


def _save_diff(client: GitHubClient, repository: str, base: str, head: str, kind: str, output: Path, local_fallback: bool, single_parent_commit: bool = False, allow_empty: bool = False) -> Path:
    folder = output / "diffs" / kind / repo_key(repository)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{base}__{head}.diff"
    if path.exists():
        return path
    if base == head:
        path.write_text("", encoding="utf-8")
        return path
    diff = download_diff(client, repository, base, head, output / ".git-cache",
                         local_fallback, single_parent_commit, allow_empty)
    temporary = path.with_suffix(".diff.tmp")
    temporary.write_text(diff, encoding="utf-8", newline="\n")
    temporary.replace(path)
    return path
