# Java GitHub Actions study

This folder contains the Java repository list, selected config-file patterns,
Java source-file pattern, mining checkpoints, and final research dataset.

Run from the project root with `GITHUB_TOKEN` set in the environment:

```bash
python -m src.data_mining \
  --repos java/repos.txt \
  --config-files java/config_files.txt \
  --source-files java/source_files.txt \
  --mining-output java/data/mining \
  --output java/final_dataset \
  --retries 5 \
  --workers 4 \
  --log-level INFO
```

The final outputs will be `java/final_dataset/episodes.csv` and
`java/final_dataset/attempts.csv`. Collection and enrichment errors are kept in
their respective JSONL files and GitHub Actions evidence is cached below the
final dataset so reruns do not download it repeatedly.

Every successful Actions API page is atomically retained under
`java/data/mining/pagination_checkpoints/`. If collection is interrupted, the
next run resumes at the first page that was not saved. `raw_runs/` remains the
authoritative complete checkpoint and is created only after the last page is
received.

To limit disk usage, each window keeps the projected resumable page data and
its count/state metadata. Only the most recent complete GitHub API response is
kept as `last_response.json`; older full responses are replaced as collection
continues.

Collection uses a fixed cutoff and recursively divides repository history into
time windows containing at most GitHub's 1,000 filtered results. This avoids
deep-pagination truncation; run IDs are deduplicated when the windows merge.
