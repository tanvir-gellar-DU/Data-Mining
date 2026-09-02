import unittest

from src.episodes import detect_episodes


def run(result, run_id, workflow=1, branch="main", sha=None, attempt=1):
    return {
        "repository": "owner/repo", "workflow_id": workflow, "workflow_name": f"wf-{workflow}",
        "branch": branch, "run_id": run_id, "run_number": run_id, "run_attempt": attempt,
        "head_sha": sha or f"sha-{run_id}", "created_at": f"2024-01-{run_id:02d}T00:00:00Z",
        "normalized_result": result,
    }


class EpisodeTests(unittest.TestCase):
    def detect(self, *results):
        return detect_episodes(run(value, index + 1) for index, value in enumerate(results))

    def test_pass_fail_pass(self):
        episodes = self.detect("PASS", "FAIL", "PASS")
        self.assertEqual(1, len(episodes))
        self.assertEqual([2], [item["run_id"] for item in episodes[0]["failures"]])

    def test_multiple_failures_form_one_episode(self):
        episodes = self.detect("PASS", "FAIL", "FAIL", "FAIL", "PASS")
        self.assertEqual([[2, 3, 4]], [[r["run_id"] for r in e["failures"]] for e in episodes])

    def test_pass_pass(self):
        self.assertEqual([], self.detect("PASS", "PASS"))

    def test_fail_pass_has_no_preceding_pass(self):
        self.assertEqual([], self.detect("FAIL", "PASS"))

    def test_unrecovered_failure_is_not_episode(self):
        self.assertEqual([], self.detect("PASS", "FAIL"))

    def test_cancelled_is_ignored_and_recorded(self):
        episode = self.detect("PASS", "CANCELLED", "FAIL", "PASS")[0]
        self.assertEqual([2], [r["run_id"] for r in episode["intervening_runs"]])
        self.assertEqual([3], [r["run_id"] for r in episode["failures"]])

    def test_skipped_between_failures_does_not_split_episode(self):
        episode = self.detect("PASS", "FAIL", "SKIPPED", "FAIL", "PASS")[0]
        self.assertEqual([2, 4], [r["run_id"] for r in episode["failures"]])
        self.assertEqual([3], [r["run_id"] for r in episode["intervening_runs"]])

    def test_workflows_are_separate(self):
        runs = [run("PASS", 1, workflow=1), run("FAIL", 2, workflow=1), run("PASS", 3, workflow=2), run("PASS", 4, workflow=1)]
        episodes = detect_episodes(runs)
        self.assertEqual(1, len(episodes))
        self.assertEqual(1, episodes[0]["workflow_id"])

    def test_branches_are_separate(self):
        runs = [run("PASS", 1), run("FAIL", 2), run("PASS", 3, branch="dev"), run("PASS", 4)]
        self.assertEqual(1, len(detect_episodes(runs)))

    def test_duplicate_sha_different_run_ids_are_preserved(self):
        runs = [run("PASS", 1), run("FAIL", 2, sha="same"), run("FAIL", 3, sha="same"), run("PASS", 4)]
        episode = detect_episodes(runs)[0]
        self.assertEqual([2, 3], [r["run_id"] for r in episode["failures"]])

    def test_rerun_attempts_are_distinct_observations(self):
        runs = [run("PASS", 1), run("FAIL", 2, sha="x", attempt=1), run("FAIL", 3, sha="x", attempt=2), run("PASS", 4)]
        episode = detect_episodes(runs)[0]
        self.assertEqual([1, 2], [r["run_attempt"] for r in episode["failures"]])


if __name__ == "__main__":
    unittest.main()

