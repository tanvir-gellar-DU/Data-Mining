"""Integrate earlier Spring run checkpoints into the six-month Java dataset.

This is a local-only, repeatable migration. It never calls GitHub or changes
the earlier complete checkpoints in java/data/mining.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Any

from .episodes import detect_episodes
from .normalize import normalize_run
from .storage import read_jsonl, repo_key, write_jsonl


SPRING_REPOSITORIES = ("spring-projects/spring-boot", "spring-projects/spring-framework")
SINCE = "2026-03-15T13:49:24Z"
UNTIL = "2026-09-15T13:49:24Z"
CSV_COLUMNS = ("repository", "default_branch", "head_sha", "processed_at",
               "collection_start", "collection_cutoff")


def _window_records(path: Path, since: str, until: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    return [record for record in read_jsonl(path)
            if since <= (record.get("created_at") or "") <= until]


def _verified_write(path: Path, records: list[dict[str, Any]]) -> None:
    if path.exists():
        if read_jsonl(path) != records:
            raise ValueError(f"Refusing to replace conflicting checkpoint: {path}")
        return
    write_jsonl(path, records)


def _write_metadata_csv(mining: Path) -> int:
    rows = []
    for path in sorted((mining / "repository_metadata").glob("*.jsonl")):
        for record in read_jsonl(path):
            rows.append({name: record.get(name, "") for name in CSV_COLUMNS})
    rows.sort(key=lambda record: record["repository"])
    target = mining / "repositories.csv"
    temporary = target.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, target)
    return len(rows)


def _remove_redundant_spring_split(target: Path, prepared: dict[str, dict[str, Any]]) -> bool:
    """Remove only the earlier generated duplicate after byte-equivalent JSON validation."""
    old_split = target.parent / "config_filter" / "extra_spring_episodes.jsonl"
    if not old_split.exists():
        return False
    expected = [item for repository in SPRING_REPOSITORIES
                for item in prepared[repository]["episodes"]]
    if read_jsonl(old_split) != expected:
        raise ValueError(f"Refusing to remove nonmatching earlier Spring split: {old_split}")
    old_split.unlink()
    return True


def merge(source: Path, target: Path, since: str = SINCE, until: str = UNTIL) -> dict[str, Any]:
    source = source.resolve()
    target = target.resolve()
    if since >= until:
        raise ValueError("since must precede until")
    if source == target:
        raise ValueError("source and target mining directories must differ")

    prepared: dict[str, dict[str, Any]] = {}
    for repository in SPRING_REPOSITORIES:
        key = repo_key(repository)
        raw_source = source / "raw_runs" / f"{key}.jsonl"
        norm_source = source / "normalized_runs" / f"{key}.jsonl"
        raw = _window_records(raw_source, since, until)
        normalized = _window_records(norm_source, since, until)
        if not raw or len(raw) != len(normalized):
            raise ValueError(f"Raw/normalized six-month run count mismatch for {repository}")
        if [run.get("run_id") for run in raw] != [run.get("run_id") for run in normalized]:
            raise ValueError(f"Raw/normalized run ID mismatch for {repository}")
        if any(normalize_run(before) != after for before, after in zip(raw, normalized)):
            raise ValueError(f"Normalization mismatch for {repository}")
        if len({run["run_id"] for run in raw}) != len(raw):
            raise ValueError(f"Duplicate run IDs for {repository}")
        older_metadata = read_jsonl(source / "repository_metadata" / f"{key}.jsonl")
        if len(older_metadata) != 1 or older_metadata[0].get("repository") != repository:
            raise ValueError(f"Missing or invalid earlier repository metadata for {repository}")
        metadata = {**older_metadata[0], "collection_start": since, "collection_cutoff": until,
                    "collection_method": "local_window_from_prior_complete_checkpoint",
                    "source_raw_checkpoint": str(raw_source)}
        episodes = detect_episodes(normalized)
        if any(item["repository"] != repository for item in episodes):
            raise ValueError(f"Episode repository mismatch for {repository}")
        prepared[repository] = {"raw": raw, "normalized": normalized,
                                "metadata": [metadata], "episodes": episodes}

    # Write all per-repository files atomically. On interruption, rerunning
    # verifies the files already written and completes the remaining steps.
    for repository, data in prepared.items():
        key = repo_key(repository)
        for folder, name in (("raw_runs", "raw"), ("normalized_runs", "normalized"),
                             ("repository_metadata", "metadata"), ("episodes", "episodes")):
            _verified_write(target / folder / f"{key}.jsonl", data[name])

    all_path = target / "episodes" / "all.jsonl"
    combined = read_jsonl(all_path)
    existing = {item["episode_id"]: item for item in combined}
    if len(existing) != len(combined):
        raise ValueError(f"Duplicate IDs in existing aggregate: {all_path}")
    for repository in SPRING_REPOSITORIES:
        for item in prepared[repository]["episodes"]:
            previous = existing.get(item["episode_id"])
            if previous is not None and previous != item:
                raise ValueError(f"Conflicting aggregate episode {item['episode_id']}")
            if previous is None:
                combined.append(item)
                existing[item["episode_id"]] = item
    write_jsonl(all_path, combined)
    repository_rows = _write_metadata_csv(target)
    removed_old_split = _remove_redundant_spring_split(target, prepared)

    result = {"window_start": since, "window_end": until, "aggregate_episodes": len(combined),
              "repository_metadata_rows": repository_rows,
              "removed_redundant_spring_split": removed_old_split,
              "spring": {repository: {"runs": len(data["raw"]), "episodes": len(data["episodes"])}
                         for repository, data in prepared.items()}}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("java/data/mining"))
    parser.add_argument("--target", type=Path, default=Path("java/six_month/data/mining"))
    parser.add_argument("--since", default=SINCE)
    parser.add_argument("--until", default=UNTIL)
    args = parser.parse_args(argv)
    print(json.dumps(merge(args.source, args.target, args.since, args.until), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
