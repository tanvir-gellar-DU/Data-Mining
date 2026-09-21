import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.git_comparison import shallow_diff, repository_cache


class ShallowDiffTests(unittest.TestCase):
    def test_exact_diff_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            def git(*args):
                return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()
            git("init", "--quiet")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            (source / "a.py").write_text("old\n")
            git("add", ".")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            (source / "a.py").write_text("new\n")
            git("commit", "-qam", "head")
            head = git("rev-parse", "HEAD")
            expected = subprocess.check_output(["git", "-C", str(source), "diff", "--binary", base, head], text=True)
            original = subprocess.run
            def local_remote(command, **kwargs):
                command = [source.as_uri() if x == "https://github.com/test/repo.git" else x for x in command]
                return original(command, **kwargs)
            cache = root / "cache"
            with patch("src.git_comparison.subprocess.run", side_effect=local_remote):
                self.assertEqual(expected, shallow_diff("test/repo", base, head, cache))
            self.assertEqual([], list(cache.iterdir()))
            with patch("src.git_comparison.subprocess.run", side_effect=local_remote) as calls:
                with repository_cache("test/repo", cache):
                    self.assertEqual(expected, shallow_diff("test/repo", base, head, cache))
                    self.assertEqual(expected, shallow_diff("test/repo", base, head, cache))
                clones = [call for call in calls.call_args_list if "clone" in call.args[0]]
                self.assertEqual(1, len(clones))
            self.assertTrue((cache / "test__repo.git").exists())

    def test_failure_cleans_temporary_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            with patch("src.git_comparison.subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")):
                with self.assertRaises(subprocess.CalledProcessError):
                    shallow_diff("test/repo", "a", "b", cache)
            self.assertEqual([], list(cache.iterdir()))

    def test_same_sha_needs_no_git(self):
        with patch("src.git_comparison.subprocess.run") as run:
            self.assertEqual("", shallow_diff("test/repo", "a", "a", Path("unused")))
            run.assert_not_called()
