# Six-month Java study (24 requested repositories)

Fixed collection window (inclusive):

- Start: `2026-03-15T13:49:24Z`
- Cutoff: `2026-09-15T13:49:24Z`

Spring Boot and Spring Framework were sliced from earlier complete local run
checkpoints to this same window. They have raw, normalized, metadata, and
episode files in `data/mining/` like the other repositories. The earlier
full-history Spring files were removed after the slices were verified.
`apache/nacos` has no completed checkpoint because collection returned 404,
so the unified episode JSONL currently represents 23 repositories.

Page/window caches are retained while a repository is incomplete. After its
raw-runs and repository-metadata checkpoints are atomically written, that
repository's pagination cache is removed automatically.

The unified command now finishes enrichment and validated per-repository CSVs
before starting the next repository. Both diff stages share one bare clone,
which is deleted after the repository's CSV checkpoint is saved. Partial combined
CSVs appear under `final_dataset/` after each completed repository, with detailed
results under `final_dataset/repositories/`. Restart an older running process to
load this behavior; reuse the same command without `--refresh`.

```bash
python -m src.data_mining \
  --repos java/six_month/repos.txt \
  --config-files java/six_month/config_files.txt \
  --source-files java/six_month/source_files.txt \
  --since 2026-03-15T13:49:24Z \
  --until 2026-09-15T13:49:24Z \
  --mining-output java/six_month/data/mining \
  --output java/six_month/final_dataset \
  --retries 5 \
  --workers 4 \
  --log-level INFO
```
