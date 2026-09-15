# Six-month Java study (23 repositories)

Fixed collection window (inclusive):

- Start: `2026-03-15T13:49:24Z`
- Cutoff: `2026-09-15T13:49:24Z`

Spring Boot and Spring Framework are intentionally excluded for now.

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
