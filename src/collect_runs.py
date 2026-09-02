from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .github_client import GitHubClient
from .storage import read_jsonl, repo_key, write_jsonl


LOG = logging.getLogger(__name__)
RUN_FIELDS = (
    "id", "run_number", "run_attempt", "status", "conclusion", "head_sha",
    "head_branch", "event", "created_at", "updated_at", "run_started_at", "html_url",
)


def _project_run(repository: str, run: dict[str, Any]) -> dict[str, Any]:
    record = {"repository": repository}
    for field in RUN_FIELDS:
        record["run_id" if field == "id" else field] = run.get(field)
    record.update({
        "workflow_id": run.get("workflow_id"),
        "workflow_name": run.get("name"),
        "branch": run.get("head_branch"),
        "pull_requests": [
            {
                "number": pr.get("number"), "url": pr.get("url"),
                "head_sha": (pr.get("head") or {}).get("sha"),
                "head_ref": (pr.get("head") or {}).get("ref"),
                "base_sha": (pr.get("base") or {}).get("sha"),
                "base_ref": (pr.get("base") or {}).get("ref"),
            } for pr in (run.get("pull_requests") or [])
        ],
    })
    return record


def collect_repository(client: GitHubClient, repository: str, output: Path, refresh: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw_path = output / "raw_runs" / f"{repo_key(repository)}.jsonl"
    metadata_path = output / "repository_metadata" / f"{repo_key(repository)}.jsonl"
    if raw_path.exists() and metadata_path.exists() and not refresh:
        LOG.info("Using checkpoint for %s", repository)
        return read_jsonl(metadata_path)[0], read_jsonl(raw_path)
    repo = client.get(f"/repos/{repository}")
    default_branch = repo.get("default_branch")
    branch = client.get(f"/repos/{repository}/branches/{default_branch}")
    metadata = {
        "repository": repository,
        "default_branch": default_branch,
        "head_sha": (branch.get("commit") or {}).get("sha"),
        "processed_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    }
    runs = [_project_run(repository, run) for run in client.paginate(f"/repos/{repository}/actions/runs", "workflow_runs")]
    runs.sort(key=lambda r: (r.get("created_at") or "", r.get("run_id") or 0))
    write_jsonl(raw_path, runs)
    write_jsonl(metadata_path, [metadata])
    LOG.info("Collected %d runs for %s", len(runs), repository)
    return metadata, runs

