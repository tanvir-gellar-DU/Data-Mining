from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path

from .collect_runs import collect_repository
from .commit_changes import enrich
from .episodes import detect_episodes
from .github_client import GitHubClient, GitHubError
from .normalize import normalize_run
from .storage import repo_key, write_jsonl


LOG = logging.getLogger(__name__)


def load_repositories(path: Path) -> list[str]:
    repositories = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        value = raw.split("#", 1)[0].strip()
        if value:
            if value.count("/") != 1:
                raise ValueError(f"Invalid owner/repository entry: {value}")
            repositories.append(value)
    return repositories


def run(args: argparse.Namespace) -> int:
    output = Path(args.output)
    client = GitHubClient(retries=args.retries)
    metadata, all_normalized, errors = [], [], []
    for repository in load_repositories(Path(args.repos)):
        try:
            meta, raw = collect_repository(client, repository, output, args.refresh)
        except GitHubError as exc:
            LOG.exception("Could not collect %s; continuing", repository)
            errors.append({"repository": repository, "error": str(exc)})
            continue
        normalized = [normalize_run(item) for item in raw]
        write_jsonl(output / "normalized_runs" / f"{repo_key(repository)}.jsonl", normalized)
        metadata.append(meta)
        all_normalized.extend(normalized)
    output.mkdir(parents=True, exist_ok=True)
    with (output / "repositories.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["repository", "default_branch", "head_sha", "processed_at"])
        writer.writeheader()
        writer.writerows(sorted(metadata, key=lambda x: x["repository"]))
    episodes = detect_episodes(all_normalized)
    by_repo: dict[str, list[dict]] = {item["repository"]: [] for item in metadata}
    for episode in episodes:
        by_repo.setdefault(episode["repository"], []).append(episode)
    for repository, records in sorted(by_repo.items()):
        write_jsonl(output / "episodes" / f"{repo_key(repository)}.jsonl", records)
    write_jsonl(output / "episodes" / "all.jsonl", episodes)
    LOG.info("Detected %d episodes", len(episodes))
    if not args.skip_enrichment and episodes:
        errors.extend(enrich(client, episodes, output, not args.no_local_diff_fallback))
    write_jsonl(output / "collection_errors.jsonl", errors)
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Mine PASS -> FAIL+ -> PASS GitHub Actions episodes")
    parser.add_argument("--repos", default="repos.txt")
    parser.add_argument("--output", default="data")
    parser.add_argument("--refresh", action="store_true", help="Ignore completed raw-run checkpoints")
    parser.add_argument("--skip-enrichment", action="store_true", help="Collect runs/episodes without commit changes and diffs")
    parser.add_argument("--no-local-diff-fallback", action="store_true")
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
