from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any, Iterable


def _sort_key(run: dict[str, Any]) -> tuple[str, int, int, int]:
    return (
        run.get("created_at") or run.get("run_started_at") or "",
        int(run.get("run_number") or 0),
        int(run.get("run_attempt") or 0),
        int(run.get("run_id") or 0),
    )


def detect_episodes(runs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        groups[(run["repository"], int(run["workflow_id"]), run.get("branch") or "")].append(run)

    episodes: list[dict[str, Any]] = []
    for (repository, workflow_id, branch), group in sorted(groups.items()):
        previous_pass = None
        failures: list[dict[str, Any]] = []
        intervening: list[dict[str, Any]] = []
        for run in sorted(group, key=_sort_key):
            result = run.get("normalized_result")
            if result == "PASS":
                if previous_pass is not None and failures:
                    identity = f"{repository}|{workflow_id}|{branch}|{previous_pass['run_id']}|{run['run_id']}"
                    episodes.append({
                        "episode_id": hashlib.sha256(identity.encode()).hexdigest()[:20],
                        "repository": repository,
                        "workflow_id": workflow_id,
                        "workflow_name": run.get("workflow_name") or previous_pass.get("workflow_name"),
                        "branch": branch,
                        "previous_pass": previous_pass,
                        "failures": failures,
                        "intervening_runs": intervening,
                        "recovery_pass": run,
                        "recovery_classification": (
                            "same_sha_recovery"
                            if failures[-1].get("head_sha") == run.get("head_sha")
                            else "code_changing_recovery"
                        ),
                    })
                previous_pass, failures, intervening = run, [], []
            elif result == "FAIL":
                if previous_pass is not None:
                    failures.append(run)
            elif previous_pass is not None:
                intervening.append(run)
    return episodes
