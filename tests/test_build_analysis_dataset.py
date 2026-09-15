import unittest

from src.build_analysis_dataset import (
    ActionsEnricher,
    _duration,
    extract_diff_paths,
    is_config_path,
    is_source_path,
    matches_config_path,
    matches_source_path,
)


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


if __name__ == "__main__":
    unittest.main()
