# Java config-episode screening

`src.filter_config_episodes` is a derived-data step. It does not change
`java/six_month/data/mining/`.

The two earlier Spring repositories are now integrated into the same
six-month mining tree as the other repositories. Their local run checkpoints
were sliced to the common inclusive window, 2026-03-15T13:49:24Z through
2026-09-15T13:49:24Z, and their episodes were added to
`data/mining/episodes/all.jsonl`. The older full-history Spring files were
removed after the six-month slices were verified. The filter reads this one
aggregate and matches only the paths in `config_files.txt`.

Run an offline first pass:

```sh
python -m src.filter_config_episodes
```

This uses already saved diffs and cached commit data. It is deliberately
conservative: a saved endpoint diff may prove a config change, but an empty
endpoint diff cannot prove that no config file was edited and later reverted.
Episodes without enough local evidence are `unresolved`, not `excluded`.

After setting `GITHUB_TOKEN` in the shell, complete uncached checks with:

```sh
python -m src.filter_config_episodes --online --workers 4
```

The screen accepts a selected config path in an existing exact endpoint diff
or in local metadata for a newly observed failed/recovery commit. The online
pass additionally checks introduced commits between consecutive observed run
SHAs. It caches compact comparison and commit-filename results under
`java/six_month/data/config_filter/cache/`, so reruns reuse successful checks.
Non-linear, incomplete, or unavailable GitHub comparisons remain unresolved;
they are never silently classified as no config change. A mixed config+source
commit qualifies. A source-only commit does not.

While the online pass is running, per-repository counts are written under
`java/six_month/data/config_filter/progress/`. The previous complete
`decisions.jsonl` and `summary.json` remain intact until the new pass finishes.

Outputs under `java/six_month/data/config_filter/`:

- `decisions.jsonl`: one `included`, `excluded`, or `unresolved` decision per
  episode, including evidence or reason.
- `selected_episodes.jsonl`: only positively verified config episodes.
- `summary.json`: totals and per-repository counts.

These are screening outputs, **not** the final Episodes and Attempts CSVs.
Those CSVs still require exact comparison and failure-evidence enrichment for
the selected episodes. `apache/nacos` remains absent because its collection
failed; this step does not recollect it.
