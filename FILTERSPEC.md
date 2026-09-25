# Config Episode Filter Specification

This document explains when to run the config episode filter, what evidence it
uses, how to run it on another machine, and how to interpret its outputs. It is
the handoff document for `src.filter_config_episodes`.

## Purpose

The mining stage detects every complete GitHub Actions sequence of:

```text
PASS -> one or more FAIL observations -> PASS
```

The config filter is the next stage. It selects episodes in which at least one
changed path matches the configured Java config filenames. A commit that
changes both a config file and Java source qualifies. An episode containing
only source changes does not qualify.

Use the filter **after episode detection and before expensive Actions
job/log enrichment and final CSV generation**:

```text
Actions runs
  -> normalize PASS/FAIL
  -> detect all episodes
  -> config episode filter
  -> enrich selected episodes
  -> build Episodes and Attempts CSVs
```

The filter does not collect Actions runs and does not detect episodes. Its
primary input is the existing aggregate episode JSONL.

## Current Java study input

The current fixed, inclusive study window is:

```text
2026-03-15T13:49:24Z .. 2026-09-15T13:49:24Z
```

The aggregate input is:

```text
java/six_month/data/mining/episodes/all.jsonl
```

It currently contains 9,392 episodes from 23 successfully collected
repositories. `apache/nacos` is absent because collection failed. Spring Boot
and Spring Framework have already been integrated into this same six-month
mining tree.

## Config path definition

Patterns are read from:

```text
java/six_month/config_files.txt
```

The current Java patterns are:

```text
pom.xml
build.gradle
build.gradle.kts
settings.gradle
gradle.properties
gradle-wrapper.properties
maven-wrapper.properties
```

A pattern without `/` matches that filename at any repository depth. For
example, `pom.xml` matches both `pom.xml` and `module-a/pom.xml`. Matching uses
repository-relative paths and preserves the real changed path as evidence.

Changing these patterns changes the research selection rule. Rerun the filter
after changing the file. The cached GitHub responses remain reusable because
they store paths rather than a previous match decision.

## Episode interval checked

For each episode, the filter orders:

1. the starting PASS;
2. all failed and intervening observations in chronological order;
3. the recovery PASS.

It checks every distinct consecutive SHA pair in that sequence. This detects a
config edit made during a failed attempt even if a later commit reverts it
before recovery. Same-SHA transitions require no comparison.

## Evidence order

The filter uses evidence in this order:

1. Existing whole-episode or repair diff files may prove that a config path is
   present.
2. Existing commit metadata for observed failure/recovery SHAs may prove that
   a config path is present.
3. Cached comparison and commit path responses are reused.
4. In online mode, GitHub's compare API retrieves the commits and net changed
   paths for each SHA pair.
5. For a complete comparison, commit API calls retrieve any additional changed
   filenames needed to check the introduced commits.

Local endpoint diffs and net comparison paths can prove a positive match. An
empty endpoint diff alone cannot prove that no config file was edited and then
reverted during the episode.

## Decision states

Every episode receives one decision:

| Status | Meaning |
| --- | --- |
| `included` | At least one selected config path was positively identified. |
| `excluded` | All required commit intervals were complete and none contained a selected config path. |
| `unresolved` | At least one required comparison was incomplete, divergent, unavailable, or returned an API error, and no positive config match was found. |

An unresolved episode must not be treated as an episode with no config change.
It requires local Git recovery, additional evidence, or manual review before a
final inclusion/exclusion decision.

The current completed run produced:

```text
screened:                 9,392
included:                 3,494
excluded:                 5,120
unresolved:                 778
failed attempts included: 8,055
```

Of the 778 unresolved episodes, 770 contain a non-linear or incomplete
comparison and 8 contain a GitHub API 404 response. These counts belong to the
current snapshot and can change when the inputs, patterns, or GitHub evidence
change.

## Files that Git does and does not provide

The source code, config definitions, tests, and this specification are in the
Git repository. The generated data is intentionally ignored by Git, including:

```text
java/six_month/data/mining/
java/six_month/data/config_filter/
```

Cloning the repository on another machine is therefore insufficient to run the
filter on the current study. Copy the data directories separately.

For a fresh online filter run, copy at minimum:

```text
java/six_month/data/mining/episodes/all.jsonl
java/six_month/data/mining/repository_metadata/
```

Also copy these when available because they reduce GitHub requests:

```text
java/six_month/data/mining/commit_changes/commits.jsonl
java/six_month/data/mining/diffs/
```

To resume the completed/current filter cache on another machine, also copy:

```text
java/six_month/data/config_filter/cache/
```

To reproduce the existing decisions without rerunning GitHub requests, copy
the entire directory:

```text
java/six_month/data/config_filter/
```

Preserve the same relative paths below the repository root.

## Setup on another machine

Requirements:

- Python 3.10 or newer;
- a clone of this repository;
- the mining data copied as described above;
- network access for online mode;
- a GitHub token for a practical API rate limit.

The project uses the Python standard library and requires no package install.
Run commands from the repository root.

Verify the current study input after copying it:

```bash
wc -l java/six_month/data/mining/episodes/all.jsonl
find java/six_month/data/mining/repository_metadata -name '*.jsonl' -type f | wc -l
```

For this snapshot, the expected counts are 9,392 episodes and 23 repository
metadata files.

## Run the filter

### 1. Optional offline pass

Run an offline pass to use only local diffs, commit records, and cached API
responses:

```bash
python -m src.filter_config_episodes --log-level INFO
```

This is useful for validating paths and recovering decisions from copied
caches. Episodes without sufficient local evidence become `unresolved`.

### 2. Online pass

Set the token without placing it in the command or shell history:

```bash
read -rs GITHUB_TOKEN
export GITHUB_TOKEN
```

Paste the token when `read` waits and press Enter. Then run:

```bash
python -m src.filter_config_episodes \
  --online \
  --workers 4 \
  --retries 5 \
  --log-level INFO
```

The token is read from `GITHUB_TOKEN` or `GH_TOKEN`. The filter needs read
access to public repository commit and comparison data. It does not modify
target repositories.

Four workers screen four repositories concurrently. Each repository has one
worker, preventing concurrent writes to the same repository cache. Reduce the
worker count if the connection is unstable or GitHub frequently throttles the
requests.

## Resume behavior

The filter stores successful comparison results under:

```text
java/six_month/data/config_filter/cache/pairs/<owner>__<repo>/
```

It stores successful commit path results under:

```text
java/six_month/data/config_filter/cache/commits/<owner>__<repo>/
```

If the process is interrupted, run the same command again. Cached SHA pairs and
commit paths are reused. The script currently walks the episode list again, but
cached checks avoid repeating completed GitHub requests.

Do not run two filter processes against the same output directory at the same
time.

## Monitor progress

While an online pass is running, per-repository progress files are written to:

```text
java/six_month/data/config_filter/progress/
```

Use this command from the repository root:

```bash
python -c 'import glob,json; a=[json.load(open(p)) for p in glob.glob("java/six_month/data/config_filter/progress/*.json")]; print("Screened:",sum(x["screened"] for x in a),"/ 9392 | Included:",sum(x["included"] for x in a),"| Excluded:",sum(x["excluded"] for x in a),"| Unresolved:",sum(x["unresolved"] for x in a))'
```

Check whether the process is running with:

```bash
pgrep -fl 'src.filter_config_episodes'
```

Progress is checkpointed every 50 episodes per repository and at repository
completion, so the displayed count can remain unchanged while requests are in
progress.

## Completion and exit status

After all repositories finish, the filter deletes the temporary progress files
and atomically writes the complete outputs. Completion is indicated by all of
the following:

- no `src.filter_config_episodes` process is running;
- `summary.json` has `"online": true`;
- `summary.json` has `"episodes_screened": 9392` for the current snapshot;
- `decisions.jsonl` contains one row per input episode.

The command returns exit status `1` when any unresolved episodes remain. This
does not mean that the run crashed. Inspect `summary.json` and
`decisions.jsonl` to distinguish a completed run with unresolved evidence from
an interrupted or failed run.

## Outputs

All filter outputs are under:

```text
java/six_month/data/config_filter/
```

| Path | Contents |
| --- | --- |
| `decisions.jsonl` | One decision per input episode, with evidence or failure reason. |
| `selected_episodes.jsonl` | Complete episode records for positively confirmed config episodes. |
| `summary.json` | Aggregate counts, per-repository counts, run mode, and API/cache metrics. |
| `cache/pairs/` | Compact SHA comparison results used for resume. |
| `cache/commits/` | Compact changed-path results for commits. |
| `progress/` | Temporary progress files present only while a run is active or interrupted. |

The filter does not create `episodes.csv` or `attempts.csv`.
`selected_episodes.jsonl` is the input for the next enrichment/CSV stage. Pass
it to the general builder with `--episodes-input`; the builder verifies that
every selected record is an exact member of the canonical mining episode file
before performing enrichment or writing CSVs.

## Validation commands

Check the finished summary:

```bash
python - <<'PY'
import json

summary = json.load(open("java/six_month/data/config_filter/summary.json"))
print(json.dumps(summary, indent=2))
assert summary["episodes_screened"] == (
    summary["included"] + summary["excluded"] + summary["unresolved"]
)
PY
```

Check output row counts:

```bash
wc -l \
  java/six_month/data/config_filter/decisions.jsonl \
  java/six_month/data/config_filter/selected_episodes.jsonl
```

For the current snapshot, these should contain 9,392 and 3,494 records,
respectively.

Run the relevant tests after changing filter behavior:

```bash
python -m unittest tests.test_filter_config_episodes -v
```

## Rules for future changes

1. Preserve the original mining episode JSONL as the full audit trail.
2. Keep mixed config and source commits when a selected config path is present.
3. Do not classify missing, incomplete, or divergent evidence as no config
   change.
4. Cache repository-relative filenames rather than only storing a Boolean match.
5. Keep one decision per input episode and validate that the three decision
   counts sum to the input episode count.
6. Do not expose GitHub tokens in source files, command arguments, logs, caches,
   or documentation.
7. Record the exact study window and config pattern file used for every run.

