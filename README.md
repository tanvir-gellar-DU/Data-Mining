# GitHub Actions break/repair miner

This repository contains one generic pipeline for every repository in `repos.txt`. Repository names are inputs only; there is no repository-specific logic and target repositories are never modified or forked.

## Generic quick start

### Requirements

- Python 3.10 or newer
- Git
- Network access to `api.github.com` and `github.com`
- A GitHub token is strongly recommended

The miner otherwise uses only Python's standard library. Run all commands from
the repository root.

### Create the input files

Every study supplies its repository and language definitions as data. Create a
study directory containing:

```text
study/
  repos.txt
  config_files.txt
  source_files.txt
```

`repos.txt` contains one `owner/repository` per line:

```text
facebook/react
vuejs/core
```

`config_files.txt` contains repository-relative configuration globs. Blank
lines and `#` comments are ignored:

```text
.github/workflows/*.yml
.github/workflows/*.yaml
package.json
tsconfig.json
tsconfig.*.json
```

`source_files.txt` defines the source language. For example:

```text
# JavaScript and TypeScript
*.js
*.jsx
*.ts
*.tsx
```

For Python it could contain `*.py` and `*.pyi`; for Java it could contain
`*.java`. Patterns without `/` match a filename at any repository depth.
Patterns containing `/` match a complete repository-relative path. No language
or repository list is built into `src/`.

### Configure GitHub authentication

The client reads `GITHUB_TOKEN`, then `GH_TOKEN`. Using an environment variable
keeps the token out of command arguments and generated files.

If GitHub CLI is already authenticated:

```powershell
$env:GITHUB_TOKEN = gh auth token
```

To enter a token without displaying it in PowerShell:

```powershell
$secureToken = Read-Host "GitHub token" -AsSecureString
$tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try {
  $env:GITHUB_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
} finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
}
```

Confirm only that it is set; do not print its value:

```powershell
if ($env:GITHUB_TOKEN) { "GITHUB_TOKEN is set" } else { "GITHUB_TOKEN is not set" }
```

A **fine-grained personal access token** is recommended. Limit repository
access to the repositories being studied and grant only:

- Actions: read-only
- Contents: read-only
- Checks: read-only
- Metadata: read-only (normally automatic)

No write, administration, workflow-write, or fork permission is used by the
miner. A classic PAT is also accepted; public-repository reads do not require
the broad `repo` scope. Private repositories require corresponding private
repository read access. Never commit a token, paste one into a config file, or
include it in a command that will be retained in shell history. Revoke any token
that is accidentally exposed.

### Set study paths and a fixed collection window

The following PowerShell variables are examples. Replace the timestamps with
the intended inclusive UTC study window, but use the same values in collection
and filtering:

```powershell
$Repos = "study/repos.txt"
$ConfigFiles = "study/config_files.txt"
$SourceFiles = "study/source_files.txt"
$Mining = "study/data/mining"
$Filter = "study/data/config_filter"
$Final = "study/final_dataset"
$Since = "2026-04-06T00:00:00Z"
$Until = "2026-10-06T23:59:59Z"
```

### Recommended configuration-focused workflow

#### 1. Collect runs and detect episodes

When only changed filenames are needed, `--skip-enrichment` avoids generating
and storing full commit patches during mining:

```powershell
python -m src.pipeline `
  --repos $Repos `
  --output $Mining `
  --since $Since `
  --until $Until `
  --skip-enrichment `
  --retries 5 `
  --log-level INFO
```

Remove `--skip-enrichment` only when full commit metadata and patch files are
required.

#### 2. Select episodes containing configured paths

An optional offline pass first uses existing local evidence and marks cases
without enough evidence as unresolved:

```powershell
python -m src.filter_config_episodes `
  --mining $Mining `
  --config-files $ConfigFiles `
  --output $Filter `
  --since $Since `
  --until $Until `
  --workers 4 `
  --retries 5 `
  --log-level INFO
```

Then run the online pass to resolve uncached comparisons:

```powershell
python -m src.filter_config_episodes `
  --mining $Mining `
  --config-files $ConfigFiles `
  --output $Filter `
  --since $Since `
  --until $Until `
  --online `
  --workers 4 `
  --retries 5 `
  --log-level INFO
```

The selected records are written to
`study/data/config_filter/selected_episodes.jsonl`.

#### 3. Build the final research CSVs

```powershell
python -m src.build_analysis_dataset `
  --input $Mining `
  --episodes-input "$Filter/selected_episodes.jsonl" `
  --config-files $ConfigFiles `
  --source-files $SourceFiles `
  --output $Final `
  --retries 5 `
  --workers 4 `
  --log-level INFO
```

This enriches failed attempts with Actions job/step/error evidence, extracts
changed filenames for the break and repair comparisons, writes the CSVs, and
validates them. Important outputs are:

```text
study/final_dataset/
  episodes.csv
  attempts.csv
  extraction_errors.jsonl
  validation_summary.json
  cache/
```

An extraction-error record preserves missing evidence; it does not necessarily
mean that the corresponding episode or attempt was discarded.

### Resume behavior

After interruption, rerun the exact same command without `--refresh`. Completed
repository collections, pagination pages, filter comparisons, Actions evidence,
and changed-path comparisons are reused. Use `--refresh` only when completed
Actions histories must intentionally be recollected.

### One-command unfiltered workflow

To collect and enrich every detected episode without the intermediate
configuration filter:

```powershell
python -m src.data_mining `
  --repos $Repos `
  --config-files $ConfigFiles `
  --source-files $SourceFiles `
  --mining-output $Mining `
  --output $Final `
  --since $Since `
  --until $Until `
  --retries 5 `
  --workers 4 `
  --log-level INFO
```

This is convenient but potentially more expensive because all detected episodes
are enriched. Use the three-stage workflow when the study should retain only
configuration-related episodes.

Useful options:

- `--refresh` ignores completed raw-run checkpoints and recollects them.
- `--skip-enrichment` stops the mining stage after normalization and episode detection.
- `--skip-actions-enrichment` omits Actions jobs/log collection during CSV generation.
- `--offline` rebuilds analysis outputs using existing local data and caches only.
- `--no-local-diff-fallback` disables Git fallback for unavailable public comparisons.
- `--retries N`, `--workers N`, and `--log-level DEBUG` control reliability, concurrency, and logging.

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

For every distinct failure or recovery SHA, commit enrichment stores the first parent (if any), file status, additions, deletions, total changes, rename source, and patch when returned. Diff enrichment first downloads a public GitHub `.diff` URL without sending the API token. A single-parent commit uses `/commit/<sha>.diff`; an episode or merge comparison uses `/compare/<base>..<head>.diff` so the requested endpoint trees are compared. Responses are cached on disk. Valid large responses are retained. An empty commit diff is accepted only when the paginated commit metadata independently reports no changed files; other empty or malformed responses use the local Git fallback unless it is disabled. Same-SHA comparisons are recorded as empty without a request. Merge commits use the first parent for the per-commit diff; root commits have no parent diff.

## Reliability

All list endpoints are paginated. Transient errors use bounded exponential retries; primary rate-limit responses wait until reset. Completed raw-run files plus metadata act as repository checkpoints, so reruns reuse them unless `--refresh` is supplied. Errors in one repository are logged to `collection_errors.jsonl`, do not stop later repositories, and cause a final nonzero exit status. Deleted workflows and branches do not matter because grouping uses IDs and branch strings embedded in historical runs.

## One-command execution details

The unified runner finishes one repository at a time, in input-list order:
collection, episode detection, commit/diff enrichment, Actions evidence, CSV
generation, and validation. Per-repository outputs are saved under
`final_dataset/repositories/<owner>__<repo>/`, and the combined CSVs are updated
after each completed repository.

On resume, matching validated per-repository results are reused. Use
`--retry-extraction-errors` to rebuild those results without recollecting raw
runs. Changes to episodes, configuration/source patterns, or enrichment settings
invalidate the corresponding final-result checkpoint. Large repositories may
still require substantial temporary disk space.

## Tests

Run the complete unit suite with:

```powershell
python -m unittest discover -s tests -v
```

The tests cover single and multiple failures, incomplete sequences, ignored
statuses, workflow/branch isolation, duplicate SHAs, rerun attempts, pagination,
comparison fallback, Actions evidence caching, and per-repository resume.
