from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any

from .episodes import detect_episodes
from .pipeline import load_repositories
from .storage import read_jsonl, repo_key, write_jsonl


SUMMARY_FIELDS = [
    "repository", "raw_run_count", "normalized_run_count", "workflow_branch_group_count",
    "episode_count", "failed_runs_inside_episodes", "same_sha_episode_count",
    "code_changing_recovery_episode_count", "code_changing_recovery_with_changed_files",
    "code_changing_recovery_with_nonempty_repair_diff", "collection_error_count", "status",
]


def run(output: Path, repos_path: Path) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    repositories = load_repositories(repos_path)
    errors = read_jsonl(output / "collection_errors.jsonl")
    errors_by_repo: dict[str, list[dict[str, Any]]] = {}
    for error in errors:
        errors_by_repo.setdefault(error["repository"], []).append(error)
    commits = {(item["repository"], item["commit_sha"]): item for item in read_jsonl(output / "commit_changes" / "commits.jsonl")}
    comparisons = {item["episode_id"]: item for item in read_jsonl(output / "diffs" / "comparisons.jsonl")}
    validation_errors: list[str] = []
    summaries = []
    code_examples = []

    for repository in repositories:
        key = repo_key(repository)
        raw = read_jsonl(output / "raw_runs" / f"{key}.jsonl")
        normalized = read_jsonl(output / "normalized_runs" / f"{key}.jsonl")
        episodes = read_jsonl(output / "episodes" / f"{key}.jsonl")
        recomputed = {item["episode_id"]: item for item in detect_episodes(normalized)}
        normalized_ids = {item["run_id"] for item in normalized}
        same_sha = code_changing = recovery_changed = repair_nonempty = failed_count = 0

        for episode in episodes:
            prefix = f"{repository}:{episode['episode_id']}"
            previous, failures, recovery = episode["previous_pass"], episode["failures"], episode["recovery_pass"]
            failed_count += len(failures)
            if previous.get("normalized_result") != "PASS": validation_errors.append(f"{prefix}: previous run is not PASS")
            if not failures or any(item.get("normalized_result") != "FAIL" for item in failures): validation_errors.append(f"{prefix}: failures list is empty or contains non-FAIL")
            if recovery.get("normalized_result") != "PASS": validation_errors.append(f"{prefix}: recovery run is not PASS")
            group = (repository, episode["workflow_id"], episode.get("branch") or "")
            all_runs = [previous, *failures, *episode.get("intervening_runs", []), recovery]
            if any((item["repository"], item["workflow_id"], item.get("branch") or "") != group for item in all_runs): validation_errors.append(f"{prefix}: mixed repository/workflow/branch")
            def order_key(item: dict[str, Any]) -> tuple[str, int, int, int]:
                return (item.get("created_at") or item.get("run_started_at") or "", int(item.get("run_number") or 0), int(item.get("run_attempt") or 0), int(item.get("run_id") or 0))
            intervening = episode.get("intervening_runs", [])
            middle = [*failures, *intervening]
            if failures != sorted(failures, key=order_key) or intervening != sorted(intervening, key=order_key):
                validation_errors.append(f"{prefix}: failure/intervening arrays are not chronological")
            if middle and (order_key(previous) >= min(map(order_key, middle)) or max(map(order_key, middle)) >= order_key(recovery)):
                validation_errors.append(f"{prefix}: surrounding PASS boundaries are not chronological")
            if any(item["run_id"] not in normalized_ids for item in all_runs): validation_errors.append(f"{prefix}: run ID absent from normalized runs")
            expected = recomputed.get(episode["episode_id"])
            if expected is None or [item["run_id"] for item in expected["failures"]] != [item["run_id"] for item in failures]: validation_errors.append(f"{prefix}: failed attempts differ from state-machine recomputation")

            last_sha, recovery_sha = failures[-1].get("head_sha"), recovery.get("head_sha")
            classification = episode.get("recovery_classification")
            expected_classification = "same_sha_recovery" if last_sha == recovery_sha else "code_changing_recovery"
            if classification != expected_classification: validation_errors.append(f"{prefix}: incorrect recovery classification")
            if expected_classification == "same_sha_recovery":
                same_sha += 1
            else:
                code_changing += 1
                recovery_commit = commits.get((repository, recovery_sha))
                has_changed_files = bool(recovery_commit and recovery_commit.get("files"))
                recovery_changed += int(has_changed_files)
                comparison = comparisons.get(episode["episode_id"])
                if not comparison or comparison.get("base_sha") != last_sha or comparison.get("head_sha") != recovery_sha:
                    validation_errors.append(f"{prefix}: missing or incorrect repair comparison SHA pair")
                    nonempty = False
                else:
                    diff_path = Path(comparison["diff_path"])
                    nonempty = diff_path.exists() and diff_path.stat().st_size > 0
                    repair_nonempty += int(nonempty)
                if not code_examples:
                    code_examples.append({**episode, "recovery_changed_files": (recovery_commit or {}).get("files", []), "repair_diff_path": (comparison or {}).get("diff_path"), "repair_diff_nonempty": nonempty})
            comparison = comparisons.get(episode["episode_id"])
            if comparison and (comparison.get("base_sha") != last_sha or comparison.get("head_sha") != recovery_sha): validation_errors.append(f"{prefix}: comparison pair does not match episode")

        repo_errors = errors_by_repo.get(repository, [])
        summaries.append({
            "repository": repository, "raw_run_count": len(raw), "normalized_run_count": len(normalized),
            "workflow_branch_group_count": len({(item["workflow_id"], item.get("branch") or "") for item in normalized}),
            "episode_count": len(episodes), "failed_runs_inside_episodes": failed_count,
            "same_sha_episode_count": same_sha, "code_changing_recovery_episode_count": code_changing,
            "code_changing_recovery_with_changed_files": recovery_changed,
            "code_changing_recovery_with_nonempty_repair_diff": repair_nonempty,
            "collection_error_count": len(repo_errors), "status": "error" if repo_errors else "ok",
        })
    return summaries, validation_errors, code_examples


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and summarize a completed mining dataset")
    parser.add_argument("--repos", default="repos.txt")
    parser.add_argument("--output", default="data/mining")
    args = parser.parse_args()
    output = Path(args.output)
    summaries, validation_errors, examples = run(output, Path(args.repos))
    with (output / "repository_summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        writer.writeheader(); writer.writerows(summaries)
    write_jsonl(output / "validation_errors.jsonl", ({"error": item} for item in validation_errors))
    write_jsonl(output / "code_changing_examples.jsonl", examples[:1])
    numeric_fields = [field for field in SUMMARY_FIELDS if field not in {"repository", "status"}]
    totals = {field: sum(int(item[field]) for item in summaries) for field in numeric_fields}
    write_jsonl(output / "validation_summary.jsonl", [{"repository_count": len(summaries), "totals": totals, "validation_error_count": len(validation_errors)}])
    print(f"repositories={len(summaries)} episodes={totals['episode_count']} validation_errors={len(validation_errors)}")
    return 1 if validation_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
