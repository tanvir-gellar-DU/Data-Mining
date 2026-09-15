from __future__ import annotations

import json
import hashlib
import logging
import os
import shutil
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .github_client import GitHubClient
from .storage import read_jsonl, repo_key, write_jsonl


LOG = logging.getLogger(__name__)
PAGE_SIZE = 100
CHECKPOINT_VERSION = 2
FILTER_RESULT_LIMIT = 1000
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


def _write_checkpoint(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _collect_run_pages(
    client: GitHubClient,
    repository: str,
    output: Path,
    refresh: bool,
    repository_created_at: str | None,
    collection_since: str | None = None,
    collection_until: str | None = None,
) -> list[dict[str, Any]]:
    """Collect all Actions runs using resumable, recursively split time windows."""
    partial = output / "pagination_checkpoints" / repo_key(repository)
    manifest_path = partial / "checkpoint.json"
    if refresh and partial.exists():
        # This directory contains only disposable collector-owned page caches.
        shutil.rmtree(partial)

    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("repository") != repository:
            raise RuntimeError(f"pagination checkpoint repository mismatch: {manifest_path}")
        if manifest.get("version") != CHECKPOINT_VERSION:
            shutil.rmtree(partial)
            manifest = {}
        else:
            cutoff = str(manifest["cutoff"])
            start = str(manifest["start"])
            if collection_since and collection_since != start:
                raise ValueError(f"checkpoint start {start} does not match requested --since {collection_since}")
            if collection_until and collection_until != cutoff:
                raise ValueError(f"checkpoint cutoff {cutoff} does not match requested --until {collection_until}")
    else:
        manifest = {}

    if not manifest:
        cutoff = collection_until or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        start = collection_since or repository_created_at or "1970-01-01T00:00:00Z"
        if datetime.fromisoformat(start.replace("Z", "+00:00")) >= datetime.fromisoformat(cutoff.replace("Z", "+00:00")):
            raise ValueError(f"collection start must precede cutoff: {start} >= {cutoff}")
        manifest = {
            "version": CHECKPOINT_VERSION,
            "repository": repository,
            "start": start,
            "cutoff": cutoff,
            "page_size": PAGE_SIZE,
            "complete": False,
        }
        _write_checkpoint(manifest_path, manifest)

    def parse_time(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def format_time(value: datetime) -> str:
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def collect_window(window_start: str, window_end: str) -> list[dict[str, Any]]:
        identity = hashlib.sha256(f"{window_start}|{window_end}".encode()).hexdigest()[:20]
        window = partial / "windows" / identity
        state_path = window / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
            "start": window_start,
            "end": window_end,
            "complete": False,
        }

        def get_page(page: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            # Older checkpoints kept every full response. Read and compact
            # those lazily; new checkpoints retain projected pages plus only
            # the repository-level rolling last_response.json.
            response_path = window / "responses" / f"{page:06d}.json"
            page_path = window / "pages" / f"{page:06d}.jsonl"
            if response_path.exists():
                payload = json.loads(response_path.read_text(encoding="utf-8"))
                projected = read_jsonl(page_path) if page_path.exists() else [
                    _project_run(repository, item) for item in payload.get("workflow_runs", [])
                ]
                if not page_path.exists():
                    write_jsonl(page_path, projected)
                if page == 1 and "total_count" not in state:
                    state["total_count"] = payload.get("total_count")
                    _write_checkpoint(state_path, state)
                response_path.unlink()
                LOG.info("Using page checkpoint %s window %s page %d (%d runs)", repository, identity, page, len(projected))
                return payload, projected
            if page_path.exists():
                projected = read_jsonl(page_path)
                payload = {"total_count": state.get("total_count")} if page == 1 else {}
                LOG.info("Using compact page checkpoint %s window %s page %d (%d runs)", repository, identity, page, len(projected))
                return payload, projected

            query = urllib.parse.urlencode({
                "created": f"{window_start}..{window_end}",
                "per_page": PAGE_SIZE,
                "page": page,
            })
            request_path = f"/repos/{repository}/actions/runs?{query}"
            payload = client.get(request_path)
            items = payload.get("workflow_runs", []) if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise RuntimeError(f"Expected workflow_runs list while collecting {repository} window {identity} page {page}")
            projected = [_project_run(repository, item) for item in items]
            if page == 1:
                state["total_count"] = payload.get("total_count")
                _write_checkpoint(state_path, state)
            write_jsonl(page_path, projected)
            _write_checkpoint(
                partial / "last_response.json",
                {
                    "repository": repository,
                    "window_start": window_start,
                    "window_end": window_end,
                    "page": page,
                    "request_path": request_path,
                    "payload": payload,
                },
            )
            LOG.info("Checkpointed %s window %s page %d (%d runs)", repository, identity, page, len(projected))
            return payload, projected

        first_payload, first_page = get_page(1)
        total_count = first_payload.get("total_count")
        if isinstance(total_count, int) and total_count > FILTER_RESULT_LIMIT:
            start_time = parse_time(window_start)
            end_time = parse_time(window_end)
            if start_time >= end_time:
                raise RuntimeError(
                    f"Cannot split dense Actions window {repository} {window_start}..{window_end} ({total_count} runs)"
                )
            midpoint = start_time + (end_time - start_time) / 2
            right_start = midpoint + timedelta(seconds=1)
            state.update({"split": True, "total_count": total_count, "midpoint": format_time(midpoint)})
            _write_checkpoint(state_path, state)
            left = collect_window(window_start, format_time(midpoint))
            right = collect_window(format_time(right_start), window_end)
            state["complete"] = True
            _write_checkpoint(state_path, state)
            return left + right

        records = list(first_page)
        page = 2
        last_page = first_page
        last_successful_page = 1
        while len(last_page) == PAGE_SIZE:
            _, projected = get_page(page)
            records.extend(projected)
            last_page = projected
            last_successful_page = page
            page += 1
        state.update({"complete": True, "total_count": total_count, "last_successful_page": last_successful_page})
        _write_checkpoint(state_path, state)
        return records

    collected = collect_window(start, cutoff)
    # Adjacent inclusive time windows and changing API pages can repeat runs.
    # Stable run IDs make merging deterministic without collapsing rerun attempts.
    unique: dict[int, dict[str, Any]] = {}
    missing_id: list[dict[str, Any]] = []
    for record in collected:
        run_id = record.get("run_id")
        if run_id is None:
            missing_id.append(record)
        else:
            unique[int(run_id)] = record
    records = list(unique.values()) + missing_id
    manifest.update({"pages_complete": True, "collected_records": len(collected), "unique_records": len(records)})
    _write_checkpoint(manifest_path, manifest)
    return records


def collect_repository(
    client: GitHubClient,
    repository: str,
    output: Path,
    refresh: bool = False,
    collection_since: str | None = None,
    collection_until: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw_path = output / "raw_runs" / f"{repo_key(repository)}.jsonl"
    metadata_path = output / "repository_metadata" / f"{repo_key(repository)}.jsonl"
    manifest_path = output / "pagination_checkpoints" / repo_key(repository) / "checkpoint.json"
    refresh_in_progress = False
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        refresh_in_progress = manifest.get("version") == CHECKPOINT_VERSION and not manifest.get("complete", False)
    if raw_path.exists() and metadata_path.exists() and not refresh and not refresh_in_progress:
        LOG.info("Using checkpoint for %s", repository)
        return read_jsonl(metadata_path)[0], read_jsonl(raw_path)
    repo = client.get(f"/repos/{repository}")
    default_branch = repo.get("default_branch")
    branch = client.get(f"/repos/{repository}/branches/{default_branch}")
    metadata = {
        "repository": repository,
        "default_branch": default_branch,
        "head_sha": (branch.get("commit") or {}).get("sha"),
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "collection_start": collection_since or repo.get("created_at"),
        "collection_cutoff": collection_until,
    }
    runs = _collect_run_pages(
        client,
        repository,
        output,
        refresh,
        repo.get("created_at"),
        collection_since,
        collection_until,
    )
    runs.sort(key=lambda r: (r.get("created_at") or "", r.get("run_id") or 0))
    write_jsonl(raw_path, runs)
    write_jsonl(metadata_path, [metadata])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["complete"] = True
    _write_checkpoint(manifest_path, manifest)
    LOG.info("Collected %d runs for %s", len(runs), repository)
    return metadata, runs
