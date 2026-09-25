# JavaScript and TypeScript GitHub Actions study

This folder contains the repository list and language-specific file patterns
for the JavaScript/TypeScript study. It reuses the generic mining pipeline in
`src/`; no repository-specific mining logic is required.

The fixed study window is six months ending on 2026-09-16 UTC:

- Start: `2026-03-16T00:00:00Z`
- End: `2026-09-16T23:59:59Z`

Source-file analysis includes only `.js`, `.jsx`, `.ts`, and `.tsx` files.

Run from the project root with `GITHUB_TOKEN` set:

```powershell
python -m src.data_mining `
  --repos Javascript/repos.txt `
  --config-files Javascript/config_files.txt `
  --source-files Javascript/source_files.txt `
  --mining-output Javascript/data/mining `
  --output Javascript/final_dataset `
  --since 2026-03-16T00:00:00Z `
  --until 2026-09-16T23:59:59Z `
  --retries 5 `
  --workers 4 `
  --log-level INFO
```

Completed Actions pages are checkpointed below
`Javascript/data/mining/pagination_checkpoints/`. A rerun without `--refresh`
resumes from those checkpoints and reuses completed repository data.

Final research outputs will be written to:

- `Javascript/final_dataset/episodes.csv`
- `Javascript/final_dataset/attempts.csv`
- `Javascript/final_dataset/extraction_errors.jsonl`
- `Javascript/final_dataset/validation_summary.json`

## Config-focused final CSVs

After `src.filter_config_episodes` has produced
`Javascript/data/config_filter/selected_episodes.jsonl`, build the final CSVs
from that verified subset without changing the complete mining episode archive:

```powershell
python -m src.build_analysis_dataset `
  --input Javascript/data/mining `
  --episodes-input Javascript/data/config_filter/selected_episodes.jsonl `
  --config-files Javascript/config_files.txt `
  --source-files Javascript/source_files.txt `
  --output Javascript/final_dataset `
  --retries 5 `
  --workers 8 `
  --log-level INFO
```

The builder verifies that every selected record is an exact member of
`data/mining/episodes/all.jsonl`. It enriches only selected failed runs. Existing
full diffs are reused, while newly fetched comparisons are stored as compact
changed-path JSON rather than complete patch bodies.

Actions enrichment is a second-stage operation: it does not recollect workflow
runs or modify `episodes/all.jsonl`. For each selected failed observation it is
keyed by repository, run ID, and `run_attempt`; it requests jobs for that exact
attempt in 30-job pages (smaller pages avoid unreliable multi-hundred-kilobyte
responses from large matrix workflows) and downloads logs only for failure-like jobs. Each returned log is
parsed in memory and discarded; full run-attempt archives are deliberately
avoided because large matrix workflows can produce multi-megabyte archives.
Only compact failed-job, failed-step, and error summary evidence is checkpointed under
`Javascript/final_dataset/cache/github_actions/`. Existing legacy jobs/check
caches are reused when their attempt identity can be verified. If an Actions log
is unavailable, check-run output is used as a fallback and the unresolved error
is retained in `extraction_errors.jsonl`.

Eight workers improve throughput for this network-bound stage. If GitHub reports
secondary-rate-limit responses, resume the same command with `--workers 4`; the
compact evidence checkpoints prevent completed attempts from being downloaded
again.

Successful 30-job pages are checkpointed under each attempt's `job_pages_30/`
directory before the next request. If a later page fails, restarting reuses
earlier pages; `jobs.json` is written only after all pages have been collected.
This reduces repeated requests after interruptions but does not reduce the
initial number of pages or guarantee faster GitHub responses.
