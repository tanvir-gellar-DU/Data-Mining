import json
import tempfile
import unittest
from pathlib import Path

from src.collect_runs import collect_repository
from src.github_client import GitHubError


def api_run(run_id):
    return {
        "id": run_id,
        "workflow_id": 1,
        "name": "CI",
        "head_branch": "main",
        "head_sha": f"sha-{run_id}",
        "created_at": f"2024-01-01T00:{run_id % 60:02d}:00Z",
    }


class FakeClient:
    def __init__(self, fail_page=None):
        self.fail_page = fail_page
        self.action_pages = []

    def get(self, path):
        if path == "/repos/owner/repo":
            return {"default_branch": "main", "created_at": "2020-01-01T00:00:00Z"}
        if path == "/repos/owner/repo/branches/main":
            return {"commit": {"sha": "head"}}
        if "/actions/runs?" in path:
            from urllib.parse import parse_qs, urlparse

            page = int(parse_qs(urlparse(path).query)["page"][0])
            self.action_pages.append(page)
            if page == self.fail_page:
                raise GitHubError("simulated interruption")
            return {
                "total_count": 101,
                "workflow_runs": [api_run(i) for i in range(1, 101)] if page == 1 else [api_run(101)],
            }
        raise AssertionError(path)


class CollectionCheckpointTests(unittest.TestCase):
    def test_failed_pagination_resumes_after_last_successful_page(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            first = FakeClient(fail_page=2)
            with self.assertRaises(GitHubError):
                collect_repository(first, "owner/repo", output)

            checkpoint = output / "pagination_checkpoints" / "owner__repo"
            pages = list((checkpoint / "windows").glob("*/pages/000001.jsonl"))
            self.assertEqual(1, len(pages))
            last_response = json.loads((checkpoint / "last_response.json").read_text())
            self.assertEqual(1, last_response["page"])
            self.assertFalse((output / "raw_runs" / "owner__repo.jsonl").exists())

            resumed = FakeClient()
            _, runs = collect_repository(resumed, "owner/repo", output)
            self.assertEqual([2], resumed.action_pages)
            self.assertEqual(101, len(runs))
            self.assertTrue((output / "raw_runs" / "owner__repo.jsonl").exists())
            manifest = json.loads((checkpoint / "checkpoint.json").read_text())
            self.assertTrue(manifest["complete"])

    def test_explicit_collection_window_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            collect_repository(
                client,
                "owner/repo",
                Path(directory),
                collection_since="2026-03-15T13:49:24Z",
                collection_until="2026-09-15T13:49:24Z",
            )
            manifest = json.loads(
                (Path(directory) / "pagination_checkpoints" / "owner__repo" / "checkpoint.json").read_text()
            )
            self.assertEqual("2026-03-15T13:49:24Z", manifest["start"])
            self.assertEqual("2026-09-15T13:49:24Z", manifest["cutoff"])


if __name__ == "__main__":
    unittest.main()
