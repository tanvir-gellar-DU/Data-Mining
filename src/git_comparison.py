"""Exact two-tree comparisons without retaining repository history."""
from pathlib import Path
import subprocess
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from .storage import repo_key


_repository_cache = ContextVar("repository_git_cache", default=None)


@contextmanager
def repository_cache(repository: str, root: Path):
    """Share one clone between mining and CSV extraction; caller owns cleanup."""
    token = _repository_cache.set((repository, root))
    try:
        yield
    finally:
        _repository_cache.reset(token)


def cached_diff(repository: str, base: str, head: str, root: Path) -> str:
    target = root / (repo_key(repository) + ".git")
    root.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        # Publish only a successful clone. Ctrl+C removes the incomplete clone.
        with tempfile.TemporaryDirectory(prefix="clone-", dir=root) as directory:
            temporary = Path(directory) / "repo.git"
            subprocess.run(["git", "clone", "--bare", "--filter=blob:none",
                            f"https://github.com/{repository}.git", str(temporary)], check=True)
            temporary.rename(target)
    missing = [sha for sha in (base, head) if subprocess.run(
        ["git", "-C", str(target), "cat-file", "-e", f"{sha}^{{commit}}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode]
    if missing:
        subprocess.run(["git", "-C", str(target), "fetch", "--quiet", "origin", *missing], check=True)
    return subprocess.run(["git", "-C", str(target), "diff", "--binary", base, head],
                          check=True, text=True, encoding="utf-8", errors="replace", capture_output=True).stdout


def shallow_diff(repository: str, base: str, head: str, cache: Path) -> str:
    if base == head:
        return ""
    shared = _repository_cache.get()
    if shared is not None:
        if shared[0] != repository:
            raise ValueError("comparison repository does not match active cache")
        return cached_diff(repository, base, head, shared[1])
    cache.mkdir(parents=True, exist_ok=True)
    # TemporaryDirectory also cleans up on errors and normal Ctrl+C.
    with tempfile.TemporaryDirectory(prefix="comparison-", dir=cache) as directory:
        def git(*args: str, capture: bool = False):
            return subprocess.run(
                ["git", "-C", directory, *args], check=True, text=True,
                encoding="utf-8", errors="replace", capture_output=capture,
            )
        git("init", "--bare", "--quiet")
        git("remote", "add", "origin", f"https://github.com/{repository}.git")
        git("config", "remote.origin.promisor", "true")
        git("config", "remote.origin.partialclonefilter", "blob:none")
        git("fetch", "--quiet", "--depth=1", "--no-tags", "--filter=blob:none",
            "origin", base, head)
        # Two-dot/tree comparison needs no ancestry or merge base.
        return git("diff", "--binary", base, head, capture=True).stdout
