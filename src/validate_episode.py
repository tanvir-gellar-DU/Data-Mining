from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .collect_runs import _project_run
from .commit_changes import enrich
from .episodes import detect_episodes
from .github_client import GitHubClient
from .normalize import normalize_run
from .storage import repo_key, write_jsonl


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate enrichment for the first episode in a bounded run window")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", default="data/episode_validation")
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--retries", type=int, default=5)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    client = GitHubClient(retries=args.retries)
    raw = []
    for page in range(1, args.pages + 1):
        payload = client.get(f"/repos/{args.repository}/actions/runs?per_page=100&page={page}")
        raw.extend(_project_run(args.repository, item) for item in payload.get("workflow_runs", []))
        if len(payload.get("workflow_runs", [])) < 100:
            break
    raw.sort(key=lambda item: (item.get("created_at") or "", item.get("run_id") or 0))
    normalized = [normalize_run(item) for item in raw]
    episodes = detect_episodes(normalized)
    if not episodes:
        raise SystemExit("No complete episode found in requested run window")
    selected = episodes[0]
    output = Path(args.output)
    key = repo_key(args.repository)
    write_jsonl(output / "raw_runs" / f"{key}.jsonl", raw)
    write_jsonl(output / "normalized_runs" / f"{key}.jsonl", normalized)
    write_jsonl(output / "episodes" / f"{key}.jsonl", [selected])
    errors = enrich(client, [selected], output, local_fallback=True)
    if errors:
        raise SystemExit(f"Enrichment errors: {errors}")
    logging.getLogger(__name__).info("Validated episode %s", selected["episode_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
