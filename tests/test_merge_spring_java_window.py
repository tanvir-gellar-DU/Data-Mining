from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.merge_spring_java_window import merge, SPRING_REPOSITORIES
from src.normalize import normalize_run
from src.storage import read_jsonl, repo_key, write_jsonl


class MergeSpringJavaWindowTests(unittest.TestCase):
    def test_local_window_merge_is_idempotent(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "old"
            target = root / "six_month"
            for repository in SPRING_REPOSITORIES:
                key = repo_key(repository)
                rows = []
                for number, (date, conclusion) in enumerate((
                    ("2026-03-14T00:00:00Z", "success"),
                    ("2026-04-01T00:00:00Z", "success"),
                    ("2026-04-02T00:00:00Z", "failure"),
                    ("2026-04-03T00:00:00Z", "success"),
                ), 1):
                    rows.append({"repository": repository, "workflow_id": 1,
                                 "workflow_name": "CI", "branch": "main", "head_sha": str(number),
                                 "run_id": number, "run_number": number, "created_at": date,
                                 "conclusion": conclusion})
                write_jsonl(source / "raw_runs" / f"{key}.jsonl", rows)
                write_jsonl(source / "normalized_runs" / f"{key}.jsonl",
                            [normalize_run(row) for row in rows])
                write_jsonl(source / "repository_metadata" / f"{key}.jsonl", [
                    {"repository": repository, "default_branch": "main", "head_sha": "abc",
                     "processed_at": "2026-09-15T00:00:00Z"}
                ])
            first = merge(source, target)
            second = merge(source, target)
            self.assertEqual(first, second)
            self.assertEqual(first["aggregate_episodes"], 2)
            self.assertEqual(first["repository_metadata_rows"], 2)
            self.assertEqual(len(read_jsonl(target / "episodes" / "all.jsonl")), 2)
            for repository in SPRING_REPOSITORIES:
                key = repo_key(repository)
                self.assertEqual(len(read_jsonl(target / "raw_runs" / f"{key}.jsonl")), 3)
                self.assertEqual(len(read_jsonl(target / "normalized_runs" / f"{key}.jsonl")), 3)
                self.assertEqual(len(read_jsonl(target / "episodes" / f"{key}.jsonl")), 1)
                self.assertEqual(len(read_jsonl(source / "raw_runs" / f"{key}.jsonl")), 4)


if __name__ == "__main__":
    unittest.main()
