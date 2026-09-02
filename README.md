# GitHub Actions break/repair miner

This repository contains one generic pipeline for every repository in `repos.txt`. Repository names are inputs only; there is no repository-specific logic and target repositories are never modified or forked.

## Setup and use

Python 3.10+ and Git are required. The miner otherwise uses only Python's standard library. A token is strongly recommended because unauthenticated GitHub API limits are small:

```powershell
$env:GITHUB_TOKEN = "github_pat_..."
python -m src.pipeline --repos repos.txt --output data
```

Useful options:

- `--refresh` redownloads repository metadata and runs instead of using the per-repository checkpoint.
- `--skip-enrichment` stops after run normalization and episode detection.
- `--no-local-diff-fallback` disables bare-clone fallback and leaves API errors visible.
- `--retries N` and `--log-level DEBUG` control reliability and logging.

The token needs read access to Actions and repository contents. No fork or write scope is used. Fork automation is deliberately outside the collection pipeline.

## Episode definition

Runs are grouped by `(repository, workflow_id, head_branch)` and sorted by creation time, run number, attempt, and run ID. Each run remains an observation: duplicate commit SHAs, reruns, and attempts are not collapsed.

An explicit state machine detects `PASS -> FAIL+ -> PASS`. All failures after the preceding pass and before the recovery pass belong to one episode. A leading failure or a trailing, unrecovered failure does not form a complete episode.

GitHub conclusions map as follows:

| GitHub conclusion | Normalized result |
| --- | --- |
| `success` | `PASS` |
| `failure` | `FAIL` |
| `cancelled` | `CANCELLED` |
| `skipped` | `SKIPPED` |
| any other non-null value | preserved verbatim |
| null/incomplete | `INCOMPLETE` |

Cancelled, skipped, neutral, timed-out, action-required, stale, startup-failure, and other outcomes are not silently treated as failures. The initial policy treats them as ignored observations: they do not start, close, or reset an episode. Ignored observations between the surrounding passes are copied to `intervening_runs`. Therefore `PASS FAIL SKIPPED FAIL PASS` is one episode with two entries in `failures` and one in `intervening_runs`.

## Outputs

```text
data/
  repositories.csv                  # original name, default branch, HEAD used, processing time
  repository_metadata/*.jsonl       # per-repository checkpoint metadata
  raw_runs/*.jsonl                  # untouched selected GitHub fields
  normalized_runs/*.jsonl           # raw fields plus normalized_result
  episodes/*.jsonl                  # per-repository records and all.jsonl
  commit_changes/commits.jsonl      # parents and changed-file metadata
  diffs/
    per_commit/...                  # parent(commit) -> commit
    repair/...                      # last failed commit -> recovery commit
    previous_pass_to_recovery/...   # previous successful commit -> recovery commit
    comparisons.jsonl               # meanings and paths
  .git-cache/                        # bare clones used only when API diff fallback is needed
```

Run records preserve repository, workflow ID/name, branch, event, run ID/number/attempt, raw status/conclusion, GitHub-provided head SHA, timestamps, URL, and PR metadata exposed by the run API. JSONL keys and record order are deterministic where practical. Empty Actions histories are valid and checkpointed.

## PR workflows and commits

For `pull_request` events, GitHub may put a synthetic merge commit in `head_sha`. The miner preserves that value exactly and never substitutes another SHA. It also stores the run API's pull-request number, head/base refs, and head/base SHAs when GitHub supplies them. Diff enrichment operates on the GitHub-provided run SHA so its meaning matches the actual workflow run.

For every distinct failure or recovery SHA, commit enrichment stores the first parent (if any), file status, additions, deletions, total changes, rename source, and patch when returned. Per-commit and cross-commit diffs first use GitHub's diff media type. Missing, empty, or suspiciously capped responses fall back to a read-only local bare clone/fetch and `git diff --binary`. Merge commits use the first parent for the per-commit diff; root commits have no parent diff.

## Reliability and tests

All list endpoints are paginated. Transient errors use bounded exponential retries; primary rate-limit responses wait until reset. Completed raw-run files plus metadata act as repository checkpoints, so reruns reuse them unless `--refresh` is supplied. Errors in one repository are logged to `collection_errors.jsonl`, do not stop later repositories, and cause a final nonzero exit status. Deleted workflows and branches do not matter because grouping uses IDs and branch strings embedded in historical runs.

Run the unit suite with:

```powershell
python -m unittest discover -v
```

The tests specify simple and multiple-failure episodes, incomplete sequences, ignored statuses, workflow/branch isolation, duplicate SHAs, and rerun attempts.
# Data-Mining
