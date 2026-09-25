import csv
import json
import tempfile
import unittest
import urllib.error
import zipfile
from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from src.build_analysis_dataset import (
    ActionsEnricher,
    _duration,
    _read_csv,
    extract_diff_paths,
    is_config_path,
    is_source_path,
    matches_config_path,
    matches_source_path,
    _load_inputs,
)
from src.storage import write_jsonl


class AnalysisDatasetTests(unittest.TestCase):
    def test_config_matcher_accepts_only_requested_patterns(self):
        accepted = [
            ".github/workflows/ci.yml",
            ".github/workflows/test.yaml",
            "requirements-dev.txt",
            "env/constraints-py311.txt",
            "pyproject.toml",
            "nested/setup.py",
            ".pre-commit-config.yaml",
            "docker/Dockerfile.cuda",
            "docker-compose.yml",
        ]
        rejected = [
            ".github/workflows/nested/ci.yml",
            ".github/dependabot.yml",
            "README.md",
            "docs/conf.py",
            "requirements.md",
            "Dockerfilefoo",
            "package.json",
        ]
        self.assertTrue(all(is_config_path(path) for path in accepted))
        self.assertFalse(any(is_config_path(path) for path in rejected))

    def test_source_matcher(self):
        self.assertTrue(is_source_path("src/main.py"))
        self.assertTrue(is_source_path("types/api.pyi"))
        self.assertFalse(is_source_path("README.md"))
        self.assertFalse(is_source_path("script.pyx"))

    def test_custom_config_matcher(self):
        patterns = ("Cargo.toml", ".github/workflows/*.yaml", "config/*.json")
        self.assertTrue(matches_config_path("nested/Cargo.toml", patterns))
        self.assertTrue(matches_config_path(".github/workflows/ci.yaml", patterns))
        self.assertTrue(matches_config_path("config/test.json", patterns))
        self.assertFalse(matches_config_path(".github/workflows/nested/ci.yaml", patterns))
        self.assertFalse(matches_config_path("nested/config/test.json", patterns))
        self.assertFalse(matches_config_path("pyproject.toml", patterns))

    def test_custom_java_source_matcher(self):
        self.assertTrue(matches_source_path("src/main/java/App.java", ("*.java",)))
        self.assertFalse(matches_source_path("src/main/kotlin/App.kt", ("*.java",)))

    def test_diff_path_parser_includes_old_and_new_names(self):
        diff = """diff --git a/src/old.py b/src/new.py
similarity index 98%
rename from src/old.py
rename to src/new.py
diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
"""
        self.assertEqual(
            ["pyproject.toml", "src/new.py", "src/old.py"],
            extract_diff_paths(diff),
        )

    def test_duration_uses_first_failure_not_start_pass(self):
        self.assertEqual(
            "2d 01:02:03",
            _duration("2024-01-01T00:00:00Z", "2024-01-03T01:02:03Z"),
        )

    def test_read_csv_accepts_fields_larger_than_python_default_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.csv"
            value = "x" * 200_000
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["large"])
                writer.writeheader()
                writer.writerow({"large": value})

            self.assertEqual([{"large": value}], _read_csv(path, ["large"]))

    def test_selected_episode_input_must_be_exact_canonical_subset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical = [
                {"episode_id": "one", "repository": "o/r", "failures": []},
                {"episode_id": "two", "repository": "o/r", "failures": []},
            ]
            selected = root / "selected.jsonl"
            write_jsonl(root / "episodes" / "all.jsonl", canonical)
            write_jsonl(selected, [canonical[1]])
            episodes, _, _ = _load_inputs(root, selected)
            self.assertEqual(["two"], [item["episode_id"] for item in episodes])

            write_jsonl(selected, [{**canonical[1], "repository": "changed/repo"}])
            with self.assertRaisesRegex(ValueError, "differs from canonical"):
                _load_inputs(root, selected)

    def test_check_summary_prefers_failure_annotations(self):
        check = {"output": {"title": "Generic title", "summary": "Generic summary"}}
        annotations = [{
            "annotation_level": "failure",
            "path": "tests/test_api.py",
            "start_line": 42,
            "title": "pytest",
            "message": "AssertionError: expected 2, got 3",
            "raw_details": "assert 3 == 2",
        }]
        self.assertEqual(
            "tests/test_api.py:42 — pytest — AssertionError: expected 2, got 3 — assert 3 == 2",
            ActionsEnricher._summary_from_check(check, annotations),
        )

    def test_check_summary_falls_back_to_output(self):
        check = {"output": {"title": "Build failed", "summary": "Compiler error", "text": "exit code 2"}}
        self.assertEqual(
            "Build failed | Compiler error | exit code 2",
            ActionsEnricher._summary_from_check(check, []),
        )

    def test_actions_enrichment_uses_exact_attempt_and_compact_checkpoint(self):
        class Client:
            token = "token"
            retries = 0
            timeout = 10
            base_url = "https://api.github.com"

            def __init__(self):
                self.paths = []

            def get(self, path):
                self.paths.append(path)
                return {"jobs": [{
                    "id": 99,
                    "run_attempt": 2,
                    "name": "tests",
                    "conclusion": "failure",
                    "steps": [{"number": 3, "name": "npm test", "conclusion": "failure"}],
                }]}

        with tempfile.TemporaryDirectory() as directory:
            client = Client()
            enricher = ActionsEnricher(Path(directory), client, offline=False)
            with patch.object(enricher, "_job_log_summaries", return_value=({99: "AssertionError"}, [])):
                evidence, errors = enricher.enrich("owner/repo", 1234, 2)

            self.assertEqual([], errors)
            self.assertEqual("tests", evidence["Failed Job"])
            self.assertEqual("tests -> npm test", evidence["Failed Step"])
            self.assertEqual("tests -> AssertionError", evidence["Error Summary"])
            self.assertEqual(
                ["/repos/owner/repo/actions/runs/1234/attempts/2/jobs?per_page=30&page=1"],
                client.paths,
            )
            self.assertTrue(
                (Path(directory) / "cache/github_actions/owner__repo/1234/attempts/2/evidence.json").exists()
            )

            # The completed evidence checkpoint prevents any repeat API work.
            second, second_errors = enricher.enrich("owner/repo", 1234, 2)
            self.assertEqual(evidence, second)
            self.assertEqual(errors, second_errors)
            self.assertEqual(1, len(client.paths))

    def test_jobs_resume_after_failed_page(self):
        with tempfile.TemporaryDirectory() as directory:
            client = unittest.mock.Mock()
            first = {"jobs": [{"id": n, "run_attempt": 1} for n in range(30)]}
            client.get.side_effect = [first, RuntimeError("temporary failure")]
            enricher = ActionsEnricher(Path(directory), client, offline=False)
            with self.assertRaisesRegex(RuntimeError, "temporary failure"):
                enricher._jobs("owner/repo", 1234, 1)
            client.get.reset_mock()
            client.get.side_effect = [{"jobs": [{"id": 30, "run_attempt": 1}]}]
            jobs = enricher._jobs("owner/repo", 1234, 1)
            self.assertEqual(list(range(31)), [job["id"] for job in jobs])
            client.get.assert_called_once_with(
                "/repos/owner/repo/actions/runs/1234/attempts/1/jobs?per_page=30&page=2"
            )

    def test_failed_job_log_is_reduced_to_error_summary(self):
        class Client:
            token = "token"
            retries = 0
            timeout = 10
            base_url = "https://api.github.com"

        archive_bytes = BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("tests/3_Run tests.txt", "line\n##[error]expected true, received false\n")
            archive.writestr("lint/2_Run lint.txt", "completed successfully\n")

        with tempfile.TemporaryDirectory() as directory:
            enricher = ActionsEnricher(Path(directory), Client(), offline=False)
            jobs = [{"id": 99, "name": "tests"}]
            with patch.object(enricher, "_download_job_log", return_value=archive_bytes.getvalue()) as download:
                summaries, errors = enricher._job_log_summaries("owner/repo", 1234, 2, jobs)

            self.assertEqual({99: "expected true, received false"}, summaries)
            self.assertEqual([], errors)
            download.assert_called_once_with("owner/repo", 99)
            cache = Path(directory) / "cache/github_actions/owner__repo/1234/attempts/2"
            self.assertTrue((cache / "log_summaries.json").exists())
            self.assertFalse(any(cache.glob("*.zip")))
            self.assertFalse(any(cache.glob("*.log")))

    def test_legacy_filter_all_cache_is_migrated_by_attempt(self):
        class Client:
            token = "token"
            retries = 0
            timeout = 10
            base_url = "https://api.github.com"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "cache/github_actions/owner__repo/1234/jobs.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(
                '{"repository":"owner/repo","run_id":1234,"jobs":['
                '{"id":1,"run_attempt":1,"name":"old","conclusion":"failure"},'
                '{"id":2,"run_attempt":2,"name":"new","conclusion":"failure"}]}'
                "\n",
                encoding="utf-8",
            )
            enricher = ActionsEnricher(root, Client(), offline=True)
            jobs = enricher._jobs("owner/repo", 1234, 2)

            self.assertEqual([2], [job["id"] for job in jobs])
            migrated = root / "cache/github_actions/owner__repo/1234/attempts/2/jobs.json"
            self.assertTrue(migrated.exists())
            self.assertEqual("legacy_filter_all_cache", json.loads(migrated.read_text())["source"])

    def test_unavailable_signed_log_blob_is_not_retried(self):
        class Client:
            token = "token"
            retries = 5
            timeout = 10
            base_url = "https://api.github.com"

            @staticmethod
            def request_slot():
                return nullcontext()

        for status in (403, 404, 410):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                redirect = urllib.error.HTTPError(
                    "https://api.github.com/log",
                    302,
                    "Found",
                    {"Location": "https://storage.example/log"},
                    BytesIO(b""),
                )
                unavailable = urllib.error.HTTPError(
                    "https://storage.example/log",
                    status,
                    "Unavailable",
                    {},
                    BytesIO(b"The specified blob does not exist"),
                )
                opener = unittest.mock.Mock()
                opener.open.side_effect = redirect
                enricher = ActionsEnricher(Path(directory), Client(), offline=False)
                with patch("urllib.request.build_opener", return_value=opener), patch(
                    "urllib.request.urlopen", side_effect=unavailable
                ) as storage_open:
                    with self.assertRaisesRegex(
                        RuntimeError, f"GitHub API {status}.*signed log blob unavailable"
                    ):
                        enricher._download_job_log("owner/repo", 99)

                self.assertEqual(1, opener.open.call_count)
                self.assertEqual(1, storage_open.call_count)


if __name__ == "__main__":
    unittest.main()
