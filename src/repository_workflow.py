"""Finish and checkpoint one repository before starting the next."""
from argparse import Namespace
import hashlib
import json
import logging
from pathlib import Path
import shutil

from .build_analysis_dataset import (
    build, _read_csv, _write_csv, _atomic_text, EPISODE_COLUMNS, ATTEMPT_COLUMNS,
    validate_outputs,
)
from .collect_runs import collect_repository
from .commit_changes import enrich
from .episodes import detect_episodes
from .github_client import GitHubClient, GitHubError
from .git_comparison import repository_cache
from .normalize import normalize_run
from .pipeline import load_repositories
from .storage import read_jsonl, write_jsonl, repo_key

LOG = logging.getLogger(__name__)
ARTIFACTS = ("episodes.csv", "attempts.csv", "extraction_errors.jsonl",
             "validation_summary.json", "mining_errors.jsonl")


def fingerprint(args, episodes):
    settings = {name: getattr(args, name, None) for name in
                ("since", "until", "skip_actions_enrichment", "no_local_diff_fallback")}
    for name in ("config_files", "source_files"):
        path = getattr(args, name, None)
        settings[name] = Path(path).read_text() if path else None
    return hashlib.sha256(json.dumps([1, settings, episodes], sort_keys=True).encode()).hexdigest()


def artifact_hashes(folder):
    return {name: hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in ARTIFACTS}


def completed(folder, signature):
    try:
        marker = json.loads((folder / "complete.json").read_text())
        return marker["fingerprint"] == signature and marker["artifacts"] == artifact_hashes(folder)
    except (OSError, ValueError, KeyError):
        return False


def publish(output, parts, episodes, requested):
    """Validate and publish aggregate CSVs after every finished repository."""
    erows, arows, errors, mining_errors = [], [], [], []
    for folder in parts:
        erows.extend(_read_csv(folder / "episodes.csv", EPISODE_COLUMNS))
        arows.extend(_read_csv(folder / "attempts.csv", ATTEMPT_COLUMNS))
        errors.extend(read_jsonl(folder / "extraction_errors.jsonl"))
        mining_errors.extend(read_jsonl(folder / "mining_errors.jsonl"))
    audit = {e["episode_id"]: {
        "complete": (e["previous_pass"]["head_sha"], e["recovery_pass"]["head_sha"]),
        "break": (e["previous_pass"]["head_sha"], e["failures"][0]["head_sha"]),
        "repair": (e["failures"][-1]["head_sha"], e["recovery_pass"]["head_sha"]),
    } for e in episodes}
    summary = validate_outputs(episodes, erows, arows, audit, errors)
    summary.update(processed_repositories=len(parts), requested_repositories=requested,
                   partial=len(parts) != requested, mining_errors=len(mining_errors))
    _write_csv(output / "episodes.csv", EPISODE_COLUMNS, erows)
    _write_csv(output / "attempts.csv", ATTEMPT_COLUMNS, arows)
    write_jsonl(output / "extraction_errors.jsonl", errors)
    _atomic_text(output / "validation_summary.json", json.dumps(summary, indent=2) + "\n")
    return summary


def run_repositories(args):
    mining, output = args.mining_output.resolve(), args.output.resolve()
    repos = list(dict.fromkeys(load_repositories(args.repos)))
    client = GitHubClient(token=args.token, retries=args.retries, timeout=args.timeout)
    parts, finished_episodes, errors = [], [], []
    status = 0
    # Preserve already detected histories while updating one repository at a time.
    all_episodes = read_jsonl(mining / "episodes" / "all.jsonl")
    for index, repository in enumerate(repos, 1):
        key = repo_key(repository)
        folder = output / "repositories" / key
        LOG.info("Repository %d/%d: %s (collection through final CSVs)", index, len(repos), repository)
        try:
            _, raw = collect_repository(client, repository, mining, args.refresh, args.since, args.until)
        except GitHubError as exc:
            errors.append({"repository": repository, "stage": "collection", "error": str(exc)})
            write_jsonl(mining / "collection_errors.jsonl", errors)
            LOG.error("Collection failed for %s: %s", repository, exc)
            status = 1
            continue
        normalized = [normalize_run(run) for run in raw]
        episodes = detect_episodes(normalized)
        write_jsonl(mining / "normalized_runs" / f"{key}.jsonl", normalized)
        write_jsonl(mining / "episodes" / f"{key}.jsonl", episodes)
        all_episodes = [e for e in all_episodes if e["repository"] != repository] + episodes
        write_jsonl(mining / "episodes" / "all.jsonl", all_episodes)
        signature = fingerprint(args, episodes)
        if args.refresh or getattr(args, "retry_extraction_errors", False) or not completed(folder, signature):
            # A failed rebuild must not leave a usable completion marker.
            (folder / "complete.json").unlink(missing_ok=True)
            with repository_cache(repository, mining / ".git-cache"):
                repo_errors = enrich(client, episodes, mining, not args.no_local_diff_fallback,
                                     preserve_other_repositories=True) if episodes else []
                write_jsonl(folder / "mining_errors.jsonl", repo_errors)
                build(Namespace(
                    input=mining, output=folder, repository=repository,
                    episodes_input=None,
                    config_files=args.config_files, source_files=args.source_files,
                    token=args.token, retries=args.retries, timeout=args.timeout,
                    workers=args.workers, offline=False,
                    no_local_diff_fallback=args.no_local_diff_fallback,
                    skip_actions_enrichment=args.skip_actions_enrichment,
                ))
            _atomic_text(folder / "complete.json", json.dumps({
                "fingerprint": signature, "artifacts": artifact_hashes(folder),
            }, sort_keys=True) + "\n")
        else:
            LOG.info("Reusing validated final CSV checkpoint for %s", repository)
        # Cleanup only after all durable outputs and validation have succeeded.
        cache = mining / ".git-cache" / f"{key}.git"
        if cache.exists():
            shutil.rmtree(cache)
            LOG.info("Removed finished Git cache for %s", repository)
        repo_errors = read_jsonl(folder / "mining_errors.jsonl")
        extraction_errors = read_jsonl(folder / "extraction_errors.jsonl")
        errors.extend(repo_errors)
        if repo_errors or extraction_errors:
            status = 1
        parts.append(folder)
        finished_episodes.extend(episodes)
        summary = publish(output, parts, finished_episodes, len(repos))
        write_jsonl(mining / "collection_errors.jsonl", errors)
        LOG.info("Finished %s; aggregate: %d episodes, %d attempts, %d/%d repositories",
                 repository, summary["episodes"], summary["attempts"], len(parts), len(repos))
    if not parts:
        publish(output, [], [], len(repos))
    write_jsonl(mining / "collection_errors.jsonl", errors)
    metadata = [row for repo in repos for row in read_jsonl(
        mining / "repository_metadata" / f"{repo_key(repo)}.jsonl")]
    _write_csv(mining / "repositories.csv", ["repository", "default_branch", "head_sha",
               "processed_at", "collection_start", "collection_cutoff"], metadata)
    return status
