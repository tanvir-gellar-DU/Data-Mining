import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.build_analysis_dataset import ComparisonProvider
from src.direct_diffs import download_diff
from src.github_client import GitHubClient, GitHubError


DIFF = (
    "diff --git a/pom.xml b/pom.xml\n"
    "--- a/pom.xml\n"
    "+++ b/pom.xml\n"
    "@@ -1 +1 @@\n-old\n+new\n"
)


class DirectDiffTests(unittest.TestCase):
    def test_commit_and_exact_endpoint_urls(self):
        client = Mock()
        client.get_public_diff.return_value = DIFF
        with patch("src.direct_diffs.shallow_diff") as git:
            self.assertEqual(
                DIFF,
                download_diff(
                    client, "owner/repo", "base", "head", Path("unused"),
                    single_parent_commit=True,
                ),
            )
            client.get_public_diff.assert_called_with(
                "https://github.com/owner/repo/commit/head.diff"
            )
            self.assertEqual(
                DIFF,
                download_diff(client, "owner/repo", "base", "head", Path("unused")),
            )
            client.get_public_diff.assert_called_with(
                "https://github.com/owner/repo/compare/base..head.diff"
            )
            git.assert_not_called()

    def test_invalid_responses_fallback_or_error(self):
        for response in ("", "<html>Error</html>"):
            with self.subTest(size=len(response)):
                client = Mock()
                client.get_public_diff.return_value = response
                with patch("src.direct_diffs.shallow_diff", return_value=DIFF) as git:
                    self.assertEqual(
                        DIFF,
                        download_diff(client, "o/r", "b", "h", Path("unused")),
                    )
                    git.assert_called_once()
                with self.assertRaises(GitHubError):
                    download_diff(
                        client, "o/r", "b", "h", Path("unused"),
                        local_fallback=False,
                    )

    def test_confirmed_empty_and_large_public_diffs_need_no_git(self):
        client = Mock()
        with patch("src.direct_diffs.shallow_diff") as git:
            client.get_public_diff.return_value = ""
            self.assertEqual(
                "",
                download_diff(
                    client, "o/r", "b", "h", Path("unused"), allow_empty=True,
                ),
            )
            large = DIFF + "x" * 1_000_000
            client.get_public_diff.return_value = large
            self.assertEqual(large, download_diff(client, "o/r", "b", "h", Path("unused")))
            git.assert_not_called()

    def test_same_sha_and_http_failure(self):
        client = Mock()
        client.get_public_diff.side_effect = GitHubError("404")
        self.assertEqual("", download_diff(client, "o/r", "a", "a", Path("unused")))
        client.get_public_diff.assert_not_called()
        with self.assertRaises(GitHubError):
            download_diff(
                client, "o/r", "a", "b", Path("unused"),
                local_fallback=False,
            )

    def test_public_request_omits_authorization(self):
        client = GitHubClient(token="test-secret")
        response = Mock()
        response.read.return_value = DIFF.encode()
        response.headers = {}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("urllib.request.urlopen", return_value=response) as request:
            self.assertEqual(
                DIFF,
                client.get_public_diff("https://github.com/o/r/commit/a.diff"),
            )
            self.assertFalse(request.call_args.args[0].has_header("Authorization"))

    def test_builder_downloads_and_reuses_cached_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = ComparisonProvider(root / "input", root / "output", False)
            provider.client = Mock()
            provider.client.get_public_diff.return_value = DIFF
            provider.local_fallback = False
            self.assertEqual(["pom.xml"], provider.exact_paths("o/r", "a", "b"))
            self.assertEqual(["pom.xml"], provider.exact_paths("o/r", "a", "b"))
            provider.client.get_public_diff.assert_called_once()
            cache = root / "output/cache/comparison_paths/o__r/a__b.paths.json"
            self.assertTrue(cache.exists())
            self.assertNotIn("-old", cache.read_text())
