"""Select config-related episodes without modifying the mining dataset.

An episode qualifies when a selected config path occurs in a newly observed
failed/recovery commit or a commit introduced between consecutive observed run
SHAs, from the starting PASS through recovery. GitHub comparisons must be
complete and ancestry-linear to prove a negative. Existing full endpoint diffs
can prove a positive, but not a negative: a file may have been edited and then
reverted during the episode.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .build_analysis_dataset import extract_diff_paths, load_config_patterns, matches_config_path
from .episodes import _sort_key
from .github_client import GitHubClient, GitHubError
from .storage import read_jsonl, repo_key, write_jsonl


LOG = logging.getLogger(__name__)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _paths_from_files(files: list[dict[str, Any]]) -> list[str]:
    return sorted({str(item[name]) for item in files for name in ("filename", "previous_filename")
                   if item.get(name)})


def _all_runs(episode: dict[str, Any]) -> list[dict[str, Any]]:
    middle = sorted([*episode["failures"], *episode.get("intervening_runs", [])], key=_sort_key)
    return [episode["previous_pass"], *middle, episode["recovery_pass"]]


def _pairs(episode: dict[str, Any]) -> list[tuple[str, str]]:
    shas = [str(run.get("head_sha") or "") for run in _all_runs(episode)]
    if not all(shas):
        raise ValueError("episode has a run without head_sha")
    return list(dict.fromkeys((base, head) for base, head in zip(shas, shas[1:]) if base != head))


def _positive_local_diff(mining: Path, episode: dict[str, Any], patterns: tuple[str, ...]) -> str | None:
    """An existing whole-episode or repair diff can prove presence, not absence."""
    start = episode["previous_pass"].get("head_sha")
    last_fail = episode["failures"][-1].get("head_sha")
    recovery = episode["recovery_pass"].get("head_sha")
    comparisons = (
        ("previous_pass_to_recovery", start, recovery),
        ("repair", last_fail, recovery),
    )
    for kind, base, head in comparisons:
        if not base or not head or base == head:
            continue
        path = mining / "diffs" / kind / repo_key(episode["repository"]) / f"{base}__{head}.diff"
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith(("diff --git ", "rename from ", "rename to ")):
                    for changed in extract_diff_paths(line):
                        if matches_config_path(changed, patterns):
                            return changed
    return None


class FilenameEvidence:
    def __init__(self, mining: Path, cache: Path, patterns: tuple[str, ...],
                 client: GitHubClient | None):
        self.cache = cache
        self.patterns = patterns
        self.client = client
        self.local_commits = {
            (row["repository"], row["commit_sha"]): _paths_from_files(row.get("files") or [])
            for row in read_jsonl(mining / "commit_changes" / "commits.jsonl")
        }
        self.metrics: Counter[str] = Counter()

    def _cached(self, kind: str, repository: str, key: str) -> Path:
        return self.cache / kind / repo_key(repository) / f"{key}.json"

    def commit_paths(self, repository: str, sha: str) -> list[str]:
        local = self.local_commits.get((repository, sha))
        if local is not None:
            self.metrics["local_commit_records_used"] += 1
            return local
        path = self._cached("commits", repository, sha)
        if path.exists():
            self.metrics["cached_commit_records_used"] += 1
            return json.loads(path.read_text(encoding="utf-8"))["paths"]
        if self.client is None:
            raise RuntimeError("commit filenames are not cached; online screening required")
        files: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self.client.get(f"/repos/{repository}/commits/{quote(sha, safe='')}?per_page=100&page={page}")
            items = payload.get("files")
            if not isinstance(items, list):
                raise GitHubError(f"Commit file list missing for {repository}@{sha} page {page}")
            files.extend(items)
            if len(items) < 100:
                break
            page += 1
        paths = _paths_from_files(files)
        _atomic_json(path, {"repository": repository, "sha": sha, "paths": paths})
        self.metrics["commit_api_requests"] += page
        return paths

    def introduced_commits(self, repository: str, base: str, head: str) -> tuple[list[str], bool, list[str], bool]:
        """Return commits, interval completeness, net paths, and path completeness."""
        if base == head:
            return [], True, [], True
        key = hashlib.sha256(f"{base}|{head}".encode()).hexdigest()[:24]
        path = self._cached("pairs", repository, key)
        if path.exists():
            self.metrics["cached_pairs_used"] += 1
            saved = json.loads(path.read_text(encoding="utf-8"))
            return (saved["commits"], saved["complete"], saved.get("net_paths", []),
                    saved.get("one_commit_files_complete", False))
        if self.client is None:
            raise RuntimeError("commit comparison is not cached; online screening required")
        commits: list[str] = []
        total: int | None = None
        linear = True
        net_paths: list[str] = []
        one_commit_files_complete = False
        page = 1
        while True:
            payload = self.client.get(
                f"/repos/{repository}/compare/{quote(base, safe='')}...{quote(head, safe='')}"
                f"?per_page=100&page={page}"
            )
            items = payload.get("commits")
            if not isinstance(items, list):
                raise GitHubError(f"Compare commit list missing for {repository} {base}..{head}")
            commits.extend(str(item["sha"]) for item in items)
            if page == 1:
                total = payload.get("total_commits")
                linear = payload.get("behind_by") == 0
                files = payload.get("files")
                if isinstance(files, list):
                    net_paths = _paths_from_files(files)
                    # GitHub may cap comparison file lists. Fewer than 300
                    # entries on a one-commit comparison are sufficient to
                    # avoid a second request for that commit's filenames.
                    one_commit_files_complete = total == 1 and len(files) < 300
            if len(items) < 100:
                break
            page += 1
        unique = list(dict.fromkeys(commits))
        complete = linear and isinstance(total, int) and len(unique) == total
        _atomic_json(path, {"repository": repository, "base": base, "head": head,
                            "commits": unique, "complete": complete, "total_commits": total,
                            "linear": linear, "net_paths": net_paths,
                            "one_commit_files_complete": one_commit_files_complete})
        self.metrics["compare_api_requests"] += page
        return unique, complete, net_paths, one_commit_files_complete

    def screen(self, mining: Path, episode: dict[str, Any]) -> dict[str, Any]:
        repository = episode["repository"]
        decision: dict[str, Any] = {"episode_id": episode["episode_id"], "repository": repository}
        local_match = _positive_local_diff(mining, episode, self.patterns)
        if local_match:
            return {**decision, "status": "included", "config_path": local_match,
                    "evidence": "existing_complete_endpoint_diff"}
        # A newly observed failed/recovery SHA with local commit metadata can
        # also prove a config-bearing commit, even if later commits revert it.
        start_sha = episode["previous_pass"].get("head_sha")
        for run in _all_runs(episode)[1:]:
            sha = run.get("head_sha")
            if not sha or sha == start_sha:
                continue
            for changed in self.local_commits.get((repository, sha), []):
                if matches_config_path(changed, self.patterns):
                    return {**decision, "status": "included", "config_path": changed,
                            "commit_sha": sha, "evidence": "existing_observed_commit_metadata"}
        try:
            pairs = _pairs(episode)
        except ValueError as exc:
            return {**decision, "status": "unresolved", "reason": str(exc)}
        uncertain: list[str] = []
        for base, head in pairs:
            try:
                commits, complete, net_paths, one_commit_files_complete = self.introduced_commits(repository, base, head)
                if not complete:
                    uncertain.append(f"non-linear or incomplete comparison {base}..{head}")
                for changed in net_paths:
                    if matches_config_path(changed, self.patterns):
                        return {**decision, "status": "included", "config_path": changed,
                                "evidence": "exact_comparison_filename"}
                if not complete:
                    # More commit-file calls cannot establish a safe negative
                    # for a divergent or truncated comparison. Preserve the
                    # uncertainty instead of spending requests on it.
                    continue
                if complete and one_commit_files_complete:
                    continue
                for sha in commits:
                    for changed in self.commit_paths(repository, sha):
                        if matches_config_path(changed, self.patterns):
                            return {**decision, "status": "included", "config_path": changed,
                                    "commit_sha": sha, "evidence": "introduced_commit_filename"}
            except (GitHubError, RuntimeError, OSError, ValueError, KeyError) as exc:
                uncertain.append(f"{base}..{head}: {exc}")
        if uncertain:
            return {**decision, "status": "unresolved", "reason": "; ".join(uncertain[:3]),
                    "unresolved_pairs": len(uncertain)}
        return {**decision, "status": "excluded", "reason": "no selected config path in complete commit intervals"}


def run(args: argparse.Namespace) -> dict[str, Any]:
    mining = args.mining.resolve()
    output = args.output.resolve()
    patterns = load_config_patterns(args.config_files)
    existing = read_jsonl(mining / "episodes" / "all.jsonl")
    if not existing:
        raise ValueError(f"No episodes found in {mining / 'episodes' / 'all.jsonl'}")
    for repository in sorted({episode["repository"] for episode in existing}):
        metadata_path = mining / "repository_metadata" / f"{repo_key(repository)}.jsonl"
        metadata = read_jsonl(metadata_path)
        if len(metadata) != 1 or metadata[0].get("collection_start") != args.since or metadata[0].get("collection_cutoff") != args.until:
            raise ValueError(f"Collection window mismatch or missing metadata for {repository}: {metadata_path}")
    episodes = existing
    ids = [episode["episode_id"] for episode in episodes]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate episode IDs in six-month mining input")
    client = GitHubClient(retries=args.retries, timeout=args.timeout) if args.online else None
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for episode in episodes:
        grouped[episode["repository"]].append(episode)

    def screen_repository(repository: str, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Counter[str]]:
        # One worker owns each repository, so it cannot race another worker
        # while writing that repository's compact SHA-pair/commit cache.
        evidence = FilenameEvidence(mining, output / "cache", patterns, client)
        local_decisions: list[dict[str, Any]] = []
        progress = output / "progress" / f"{repo_key(repository)}.json"
        for index, episode in enumerate(items, 1):
            local_decisions.append(evidence.screen(mining, episode))
            if index % 50 == 0 or index == len(items):
                counts = Counter(row["status"] for row in local_decisions)
                _atomic_json(progress, {"repository": repository, "screened": index,
                                        "total": len(items), "included": counts["included"],
                                        "excluded": counts["excluded"], "unresolved": counts["unresolved"],
                                        "metrics": dict(evidence.metrics)})
                LOG.info("Screened %s %d/%d: included=%d excluded=%d unresolved=%d",
                         repository, index, len(items), counts["included"],
                         counts["excluded"], counts["unresolved"])
        return local_decisions, evidence.metrics

    decisions_by_id: dict[str, dict[str, Any]] = {}
    metrics: Counter[str] = Counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(screen_repository, repository, items): repository
                   for repository, items in grouped.items()}
        for future in as_completed(futures):
            local_decisions, local_metrics = future.result()
            decisions_by_id.update((row["episode_id"], row) for row in local_decisions)
            metrics.update(local_metrics)
    decisions = [decisions_by_id[episode["episode_id"]] for episode in episodes]
    selected = [episode for episode in episodes if decisions_by_id[episode["episode_id"]]["status"] == "included"]
    write_jsonl(output / "decisions.jsonl", decisions)
    write_jsonl(output / "selected_episodes.jsonl", selected)
    counts = Counter(row["status"] for row in decisions)
    by_repository: dict[str, dict[str, int]] = {}
    for row in decisions:
        repository_counts = by_repository.setdefault(row["repository"],
                                                     {"included": 0, "excluded": 0, "unresolved": 0})
        repository_counts[row["status"]] += 1
    summary = {"six_month_episodes": len(existing), "episodes_screened": len(episodes), "included": counts["included"],
               "excluded": counts["excluded"], "unresolved": counts["unresolved"],
               "selected_failed_attempts": sum(len(e["failures"]) for e in selected),
               "config_patterns": list(patterns), "since": args.since, "until": args.until,
               "online": args.online, "workers": args.workers, "metrics": dict(metrics),
               "by_repository": dict(sorted(by_repository.items()))}
    assert counts["included"] + counts["excluded"] + counts["unresolved"] == len(episodes)
    assert len(selected) == counts["included"]
    _atomic_json(output / "summary.json", summary)
    for repository in grouped:
        (output / "progress" / f"{repo_key(repository)}.json").unlink(missing_ok=True)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mining", type=Path, default=Path("java/six_month/data/mining"))
    parser.add_argument("--config-files", type=Path, default=Path("java/six_month/config_files.txt"))
    parser.add_argument("--output", type=Path, default=Path("java/six_month/data/config_filter"))
    parser.add_argument("--since", default="2026-03-15T13:49:24Z")
    parser.add_argument("--until", default="2026-09-15T13:49:24Z")
    parser.add_argument("--online", action="store_true", help="Fetch uncached commit filenames from GitHub")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent repositories to screen")
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.online and not (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")):
        raise SystemExit("--online requires GITHUB_TOKEN or GH_TOKEN in the environment")
    logging.basicConfig(level=getattr(logging, args.log_level.upper()),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    summary = run(args)
    LOG.info("Config screening summary: %s", json.dumps(summary, sort_keys=True))
    return 0 if summary["unresolved"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
