# Final analysis dataset

`episodes.csv` contains one complete PASS -> FAIL+ -> PASS episode per row.
`attempts.csv` contains one failed run per row. Multi-value cells use `; ` as the delimiter.

`Time to recovery` is `recovery_pass.created_at - first_failure.created_at` and uses `<days>d HH:MM:SS`. Source files are only `.py` and `.pyi`. Config files use only the patterns specified for this extraction. Empty comparison-derived cells are disambiguated by `extraction_errors.jsonl`; an empty cell without a corresponding comparison error means the exact diff was available and contained no matching path.

Error summaries prefer retained job-log errors, then use Check Run failure annotations, then Check Run output title/summary/text. GitHub Actions job, log, Check Run, annotation, and generated exact-diff responses are cached under `cache/`.
