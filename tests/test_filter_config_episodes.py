from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.filter_config_episodes import FilenameEvidence, _pairs
from src.storage import write_jsonl


def episode(*shas):
    def run(sha, number):
        return {"repository": "example/repo", "workflow_id": 1, "branch": "main",
                "head_sha": sha, "run_id": number, "created_at": f"2026-05-{number:02d}T00:00:00Z",
                "normalized_result": "PASS" if number in (1, len(shas)) else "FAIL"}

    runs = [run(sha, number) for number, sha in enumerate(shas, 1)]
    return {"episode_id": "sample", "repository": "example/repo", "previous_pass": runs[0],
            "failures": runs[1:-1], "intervening_runs": [], "recovery_pass": runs[-1]}


class FakeClient:
    def __init__(self, comparisons, commits):
        self.comparisons = comparisons
        self.commits = commits

    def get(self, path):
        if "/compare/" in path:
            key = path.split("/compare/", 1)[1].split("?", 1)[0]
            return self.comparisons[key]
        sha = path.split("/commits/", 1)[1].split("?", 1)[0]
        return {"files": self.commits[sha]}


class FilterConfigEpisodesTests(unittest.TestCase):
    def test_mixed_config_and_source_commit_qualifies(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = FakeClient(
                {"a...b": {"commits": [{"sha": "b"}], "total_commits": 1, "behind_by": 0},
                 "b...c": {"commits": [{"sha": "c"}], "total_commits": 1, "behind_by": 0}},
                {"b": [{"filename": "src/Main.java"}, {"filename": "module/pom.xml"}],
                 "c": [{"filename": "README.md"}]},
            )
            evidence = FilenameEvidence(root, root / "cache", ("pom.xml",), client)
            result = evidence.screen(root, episode("a", "b", "c"))
            self.assertEqual(result["status"], "included")
            self.assertEqual(result["config_path"], "module/pom.xml")

    def test_source_only_episode_is_excluded_when_every_pair_is_complete(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = FakeClient(
                {"a...b": {"commits": [{"sha": "b"}], "total_commits": 1, "behind_by": 0},
                 "b...c": {"commits": [{"sha": "c"}], "total_commits": 1, "behind_by": 0}},
                {"b": [{"filename": "src/Main.java"}], "c": [{"filename": "README.md"}]},
            )
            evidence = FilenameEvidence(root, root / "cache", ("pom.xml",), client)
            self.assertEqual(evidence.screen(root, episode("a", "b", "c"))["status"], "excluded")

    def test_one_commit_comparison_files_avoid_commit_request(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = FakeClient(
                {"a...b": {"commits": [{"sha": "b"}], "total_commits": 1, "behind_by": 0,
                             "files": [{"filename": "src/Main.java"}]},
                 "b...c": {"commits": [{"sha": "c"}], "total_commits": 1, "behind_by": 0,
                             "files": [{"filename": "README.md"}]}},
                {},
            )
            evidence = FilenameEvidence(root, root / "cache", ("pom.xml",), client)
            self.assertEqual(evidence.screen(root, episode("a", "b", "c"))["status"], "excluded")

    def test_comparison_files_prove_config_even_when_source_also_changed(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = FakeClient(
                {"a...b": {"commits": [{"sha": "x"}, {"sha": "b"}],
                             "total_commits": 2, "behind_by": 0,
                             "files": [{"filename": "module/pom.xml"},
                                       {"filename": "src/Main.java"}]}},
                {},
            )
            evidence = FilenameEvidence(root, root / "cache", ("pom.xml",), client)
            result = evidence.screen(root, episode("a", "b", "b"))
            self.assertEqual(result["status"], "included")
            self.assertEqual(result["evidence"], "exact_comparison_filename")

    def test_divergent_comparison_without_config_stays_unresolved(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = FakeClient(
                {"a...b": {"commits": [{"sha": "b"}], "total_commits": 1,
                             "behind_by": 1, "files": [{"filename": "src/Main.java"}]}},
                {},
            )
            evidence = FilenameEvidence(root, root / "cache", ("pom.xml",), client)
            result = evidence.screen(root, episode("a", "b", "b"))
            self.assertEqual(result["status"], "unresolved")

    def test_missing_pair_is_unresolved_not_excluded(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = FilenameEvidence(root, root / "cache", ("pom.xml",), None)
            self.assertEqual(evidence.screen(root, episode("a", "b", "c"))["status"], "unresolved")

    def test_existing_observed_commit_metadata_can_prove_positive(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_jsonl(root / "commit_changes" / "commits.jsonl", [
                {"repository": "example/repo", "commit_sha": "b",
                 "files": [{"filename": "src/Main.java"}, {"filename": "pom.xml"}]}
            ])
            evidence = FilenameEvidence(root, root / "cache", ("pom.xml",), None)
            result = evidence.screen(root, episode("a", "b", "c"))
            self.assertEqual(result["status"], "included")
            self.assertEqual(result["evidence"], "existing_observed_commit_metadata")

    def test_existing_endpoint_diff_can_prove_positive(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            diff = root / "diffs" / "previous_pass_to_recovery" / "example__repo" / "a__c.diff"
            diff.parent.mkdir(parents=True)
            diff.write_text("diff --git a/module/pom.xml b/module/pom.xml\n@@ -1 +1 @@\n")
            evidence = FilenameEvidence(root, root / "cache", ("pom.xml",), None)
            self.assertEqual(evidence.screen(root, episode("a", "b", "c"))["status"], "included")

    def test_repair_diff_can_prove_positive_when_complete_diff_is_empty(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            whole = root / "diffs" / "previous_pass_to_recovery" / "example__repo" / "a__c.diff"
            repair = root / "diffs" / "repair" / "example__repo" / "b__c.diff"
            whole.parent.mkdir(parents=True)
            repair.parent.mkdir(parents=True)
            whole.write_text("")
            repair.write_text("diff --git a/pom.xml b/pom.xml\n@@ -1 +1 @@\n")
            evidence = FilenameEvidence(root, root / "cache", ("pom.xml",), None)
            self.assertEqual(evidence.screen(root, episode("a", "b", "c"))["status"], "included")

    def test_pairs_preserve_episode_observation_order(self):
        self.assertEqual(_pairs(episode("a", "a", "b", "c")), [("a", "b"), ("b", "c")])

if __name__ == "__main__":
    unittest.main()
