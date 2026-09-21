from __future__ import annotations

import argparse
import ast
import csv
import fnmatch
import json
import logging
import os
import re
import threading
import urllib.parse
import urllib.error
import urllib.request
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .github_client import GitHubClient, GitHubError
from .direct_diffs import download_diff
from .storage import read_jsonl, repo_key, write_jsonl


LOG = logging.getLogger(__name__)

EPISODE_COLUMNS = [
    "Episode ID",
    "Repository",
    "GitHub URL",
    "Workflow Name",
    "Branch",
    "Start Pass SHA",
    "First Fail SHA",
    "Fail Shas",
    "Failed Attempt Count",
    "Recovery Pass SHA",
    "Time to recovery",
    "Source files changed",
    "Config files changed",
    "Break side config changes",
    "Repair side config changes",
    "Manual Validation",
    "Config Category",
    "Notes",
]

ATTEMPT_COLUMNS = [
    "Episode ID",
    "Attempt Number",
    "GitHub Action Run URL",
    "Commit SHA",
    "Timestamp",
    "Source files changed",
    "Config files changed",
    "Patch Type",
    "Failed Job",
    "Failed Step",
    "Error Summary",
    "Manual Validation",
    "Notes",
]

DELIMITER = "; "
FAILURE_LIKE_JOB_CONCLUSIONS = {"failure", "timed_out", "startup_failure", "action_required"}
ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
LOG_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s+")

DEFAULT_CONFIG_PATTERNS = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    "requirements*.txt",
    "constraints*.txt",
    "pyproject.toml",
    "poetry.lock",
    "Pipfile",
    "Pipfile.lock",
    "setup.py",
    "setup.cfg",
    "MANIFEST.in",
    "pytest.ini",
    "tox.ini",
    "mypy.ini",
    ".flake8",
    ".pylintrc",
    "ruff.toml",
    ".ruff.toml",
    ".coveragerc",
    ".pre-commit-config.yaml",
    ".pre-commit-config.yml",
    "Dockerfile",
    "Dockerfile.*",
    "docker-compose.yml",
    "docker-compose.yaml",
    ".dockerignore",
)
DEFAULT_SOURCE_PATTERNS = ("*.py", "*.pyi")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _run_timestamp(run: dict[str, Any]) -> str:
    return run.get("created_at") or run.get("run_started_at") or ""


def _duration(first_failure: str, recovery: str) -> str:
    seconds = int((_parse_time(recovery) - _parse_time(first_failure)).total_seconds())
    if seconds < 0:
        raise ValueError(f"negative recovery duration: {first_failure} -> {recovery}")
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"


def _normalise_path(path: str | None) -> str | None:
    if not path:
        return None
    value = path.replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    return value or None


def matches_source_path(path: str, patterns: Iterable[str]) -> bool:
    """Match source files with the same repository-relative glob rules as config files."""
    return matches_config_path(path, patterns)


def is_source_path(path: str) -> bool:
    return matches_source_path(path, DEFAULT_SOURCE_PATTERNS)


def load_config_patterns(path: Path | None) -> tuple[str, ...]:
    """Load repository-relative config globs, ignoring blank lines/comments."""
    if path is None:
        return DEFAULT_CONFIG_PATTERNS
    patterns = tuple(
        value
        for raw in path.read_text(encoding="utf-8").splitlines()
        if (value := raw.split("#", 1)[0].strip())
    )
    if not patterns:
        raise ValueError(f"No config-file patterns found in {path}")
    return patterns


def load_source_patterns(path: Path | None) -> tuple[str, ...]:
    if path is None:
        return DEFAULT_SOURCE_PATTERNS
    patterns = tuple(
        value
        for raw in path.read_text(encoding="utf-8").splitlines()
        if (value := raw.split("#", 1)[0].strip())
    )
    if not patterns:
        raise ValueError(f"No source-file patterns found in {path}")
    return patterns


def matches_config_path(path: str, patterns: Iterable[str]) -> bool:
    """Match paths using repository-relative globs; basename globs match at any depth."""
    value = _normalise_path(path) or ""
    for raw_pattern in patterns:
        pattern = _normalise_path(raw_pattern) or ""
        if not pattern:
            continue
        if "/" in pattern:
            # Keep a pattern such as workflows/*.yml non-recursive.
            same_depth = len(PurePosixPath(value).parts) == len(PurePosixPath(pattern).parts)
            if same_depth and fnmatch.fnmatchcase(value, pattern):
                return True
        elif fnmatch.fnmatchcase(PurePosixPath(value).name, pattern):
            return True
    return False


def is_config_path(path: str) -> bool:
    """Backward-compatible matcher using the default Python config definition."""
    return matches_config_path(path, DEFAULT_CONFIG_PATTERNS)


def _decode_git_path(token: str) -> str:
    token = token.strip()
    if token.startswith('"') and token.endswith('"'):
        try:
            return ast.literal_eval(token)
        except (SyntaxError, ValueError):
            return token[1:-1]
    return token


def extract_diff_paths(diff_text: str) -> list[str]:
    """Return old and new repository-relative paths named by a Git diff."""
    paths: set[str] = set()
    for line in diff_text.splitlines():
        candidates: list[str] = []
        if line.startswith("diff --git "):
            rest = line[len("diff --git ") :]
            # Git quotes unusual paths. The split is deliberately limited by the
            # second a/ or b/ marker rather than whitespace inside a quoted name.
            match = re.match(r'((?:"(?:\\.|[^"\\])*"|\S+))\s+((?:"(?:\\.|[^"\\])*"|\S+))$', rest)
            if match:
                candidates.extend([match.group(1), match.group(2)])
        elif line.startswith("rename from "):
            candidates.append(line[len("rename from ") :])
        elif line.startswith("rename to "):
            candidates.append(line[len("rename to ") :])
        for candidate in candidates:
            value = _decode_git_path(candidate)
            if value.startswith(("a/", "b/")):
                value = value[2:]
            value = _normalise_path(value)
            if value and value != "/dev/null":
                paths.add(value)
    return sorted(paths)


def _paths_from_commit(record: dict[str, Any]) -> list[str]:
    paths: set[str] = set()
    for item in record.get("files") or []:
        for key in ("filename", "previous_filename"):
            value = _normalise_path(item.get(key))
            if value:
                paths.add(value)
    return sorted(paths)


def _classified(paths: Iterable[str], predicate: Any) -> str:
    return DELIMITER.join(sorted({path for path in paths if predicate(path)}))


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="raise", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


class ComparisonProvider:
    def __init__(self, mining: Path, output: Path, offline: bool):
        self.mining = mining
        self.output = output
        self.offline = offline
        self.client = GitHubClient()
        self.local_fallback = True
        self.commit_records: dict[tuple[str, str], dict[str, Any]] = {}

    def set_commit_records(self, records: dict[tuple[str, str], dict[str, Any]]) -> None:
        self.commit_records = records

    def existing_paths(self, repository: str, base: str, head: str) -> list[str] | None:
        key = repo_key(repository)
        name = f"{base}__{head}.diff"
        for kind in ("per_commit", "repair", "previous_pass_to_recovery"):
            path = self.mining / "diffs" / kind / key / name
            if path.exists():
                return extract_diff_paths(path.read_text(encoding="utf-8", errors="replace"))
        cached = self.output / "cache" / "diffs" / key / name
        if cached.exists():
            return extract_diff_paths(cached.read_text(encoding="utf-8", errors="replace"))
        return None

    def exact_paths(self, repository: str, base: str, head: str) -> list[str]:
        if base == head:
            return []
        existing = self.existing_paths(repository, base, head)
        if existing is not None:
            return existing
        commit = self.commit_records.get((repository, head))
        if commit and commit.get("parent_sha") == base:
            return _paths_from_commit(commit)
        if self.offline:
            raise RuntimeError("exact comparison is not cached locally")
        return self._download_diff_paths(repository, base, head)

    def _download_diff_paths(self, repository: str, base: str, head: str) -> list[str]:
        diff = download_diff(self.client, repository, base, head,
                             self.output / "cache" / "git", self.local_fallback)
        cached = self.output / "cache" / "diffs" / repo_key(repository) / f"{base}__{head}.diff"
        _atomic_text(cached, diff)
        return extract_diff_paths(diff)


class ActionsEnricher:
    def __init__(self, output: Path, client: GitHubClient, offline: bool):
        self.output = output
        self.client = client
        self.offline = offline
        self._stop_reason: str | None = None
        self._lock = threading.Lock()
        self._seed_permanent_failures()

    def _seed_permanent_failures(self) -> None:
        """Convert prior 404/410 results into negative cache entries."""
        prior = self.output / "extraction_errors.jsonl"
        if not prior.exists():
            return
        for item in read_jsonl(prior):
            error = str(item.get("error") or "")
            permanent = (
                "GitHub API 404" in error
                or "GitHub API 410" in error
                or "HTTP Error 404: The specified blob does not exist" in error
            )
            if not permanent:
                continue
            repository = item.get("repository")
            run_id = item.get("run_id")
            if not repository or run_id is None:
                continue
            run_cache = self._run_cache(str(repository), int(run_id))
            if item.get("stage") == "actions_jobs":
                target = run_cache / "jobs.error.json"
            elif item.get("stage") == "actions_job_log" and item.get("job_id") is not None:
                target = run_cache / "logs" / f"{item['job_id']}.error.json"
            else:
                continue
            if not target.exists():
                _atomic_text(target, json.dumps({"error": error}, sort_keys=True) + "\n")

    def _run_cache(self, repository: str, run_id: int) -> Path:
        return self.output / "cache" / "github_actions" / repo_key(repository) / str(run_id)

    def _jobs(self, repository: str, run_id: int) -> list[dict[str, Any]]:
        cache = self._run_cache(repository, run_id) / "jobs.json"
        error_cache = self._run_cache(repository, run_id) / "jobs.error.json"
        if cache.exists():
            payload = json.loads(cache.read_text(encoding="utf-8"))
            return payload["jobs"]
        if error_cache.exists():
            raise RuntimeError(json.loads(error_cache.read_text(encoding="utf-8"))["error"])
        if self.offline:
            raise RuntimeError("jobs are not cached locally")
        with self._lock:
            if self._stop_reason:
                raise RuntimeError(self._stop_reason)
        jobs = list(self.client.paginate(f"/repos/{repository}/actions/runs/{run_id}/jobs?filter=all", "jobs"))
        _atomic_text(
            cache,
            json.dumps(
                {"repository": repository, "run_id": run_id, "fetched_at": datetime.now(timezone.utc).isoformat(), "jobs": jobs},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
        )
        return jobs

    def _job_log(self, repository: str, run_id: int, job_id: int) -> str:
        cache = self._run_cache(repository, run_id) / "logs" / f"{job_id}.log"
        error_cache = self._run_cache(repository, run_id) / "logs" / f"{job_id}.error.json"
        if cache.exists():
            return cache.read_text(encoding="utf-8", errors="replace")
        if error_cache.exists():
            raise RuntimeError(json.loads(error_cache.read_text(encoding="utf-8"))["error"])
        if self.offline:
            raise RuntimeError("job log is not cached locally")
        body = self._download_job_log(repository, job_id)
        if body.startswith(b"PK\x03\x04"):
            with zipfile.ZipFile(BytesIO(body)) as archive:
                pieces = [archive.read(name).decode("utf-8", "replace") for name in sorted(archive.namelist())]
            text = "\n".join(pieces)
        else:
            text = body.decode("utf-8", "replace")
        _atomic_text(cache, text)
        return text

    def _check_run(self, repository: str, run_id: int, job: dict[str, Any]) -> dict[str, Any]:
        job_id = int(job["id"])
        cache = self._run_cache(repository, run_id) / "checks" / f"{job_id}.json"
        error_cache = self._run_cache(repository, run_id) / "checks" / f"{job_id}.error.json"
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        if error_cache.exists():
            raise RuntimeError(json.loads(error_cache.read_text(encoding="utf-8"))["error"])
        if self.offline:
            raise RuntimeError("check run is not cached locally")
        url = job.get("check_run_url")
        if not url:
            raise RuntimeError("job has no check_run_url")
        try:
            payload = self.client.get(str(url))
        except Exception as exc:
            if "GitHub API 404" in str(exc) or "GitHub API 410" in str(exc):
                _atomic_text(error_cache, json.dumps({"error": str(exc)}, sort_keys=True) + "\n")
            raise
        _atomic_text(cache, json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return payload

    def _check_annotations(
        self,
        repository: str,
        run_id: int,
        job: dict[str, Any],
        check: dict[str, Any],
    ) -> list[dict[str, Any]]:
        job_id = int(job["id"])
        cache = self._run_cache(repository, run_id) / "checks" / f"{job_id}.annotations.json"
        error_cache = self._run_cache(repository, run_id) / "checks" / f"{job_id}.annotations.error.json"
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        if error_cache.exists():
            raise RuntimeError(json.loads(error_cache.read_text(encoding="utf-8"))["error"])
        annotations_count = int((check.get("output") or {}).get("annotations_count") or 0)
        if annotations_count == 0:
            _atomic_text(cache, "[]\n")
            return []
        if self.offline:
            raise RuntimeError("check run annotations are not cached locally")
        url = job.get("check_run_url")
        if not url:
            raise RuntimeError("job has no check_run_url")
        try:
            annotations = list(self.client.paginate(f"{url}/annotations"))
        except Exception as exc:
            if "GitHub API 404" in str(exc) or "GitHub API 410" in str(exc):
                _atomic_text(error_cache, json.dumps({"error": str(exc)}, sort_keys=True) + "\n")
            raise
        _atomic_text(cache, json.dumps(annotations, ensure_ascii=False, sort_keys=True) + "\n")
        return annotations

    @staticmethod
    def _clean_evidence_text(value: Any, limit: int = 800) -> str:
        if value is None:
            return ""
        text = ANSI_ESCAPE.sub("", str(value))
        text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", text)
        text = re.sub(r"[`*#>|]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit].rstrip()

    @classmethod
    def _summary_from_check(cls, check: dict[str, Any], annotations: list[dict[str, Any]]) -> str:
        failures = [item for item in annotations if item.get("annotation_level") == "failure"]
        messages: list[str] = []
        for item in failures[:3]:
            location = cls._clean_evidence_text(item.get("path"), 240)
            if item.get("start_line"):
                location += f":{item['start_line']}"
            title = cls._clean_evidence_text(item.get("title"), 240)
            message = cls._clean_evidence_text(item.get("message"), 600)
            details = cls._clean_evidence_text(item.get("raw_details"), 600)
            pieces = [value for value in (location, title, message) if value]
            if details and details != message:
                pieces.append(details)
            rendered = " — ".join(pieces)
            if rendered and rendered not in messages:
                messages.append(rendered)
        if messages:
            return " | ".join(messages)[:1000]

        output = check.get("output") or {}
        title = cls._clean_evidence_text(output.get("title"), 250)
        summary = cls._clean_evidence_text(output.get("summary"), 700)
        text = cls._clean_evidence_text(output.get("text"), 700)
        pieces: list[str] = []
        for value in (title, summary, text):
            if value and value not in pieces:
                pieces.append(value)
        return " | ".join(pieces)[:1000]

    def _check_error_summary(
        self,
        repository: str,
        run_id: int,
        job: dict[str, Any],
    ) -> str:
        check = self._check_run(repository, run_id, job)
        annotations = self._check_annotations(repository, run_id, job, check)
        return self._summary_from_check(check, annotations)

    def _download_job_log(self, repository: str, job_id: int) -> bytes:
        """Download a job log without leaking GitHub auth to signed storage."""
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
                return None

        url = f"{self.client.base_url}/repos/{repository}/actions/jobs/{job_id}/logs"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "github-ci-episode-miner/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.client.token:
            headers["Authorization"] = f"Bearer {self.client.token}"
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=self.client.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308} and exc.headers.get("Location"):
                # The Location is a short-lived signed URL. Do not forward the
                # GitHub Authorization header to that different host.
                storage = urllib.request.Request(
                    exc.headers["Location"],
                    headers={"User-Agent": "github-ci-episode-miner/1.0"},
                )
                with urllib.request.urlopen(storage, timeout=self.client.timeout) as response:
                    return response.read()
            body = exc.read().decode("utf-8", "replace")
            raise GitHubError(f"GitHub API {exc.code} for {url}: {body[:500]}") from exc

    @staticmethod
    def _error_from_log(text: str) -> str:
        strong: list[str] = []
        fallback: list[str] = []
        for raw in text.splitlines():
            line = ANSI_ESCAPE.sub("", raw).strip()
            line = LOG_TIMESTAMP.sub("", line).strip()
            if not line:
                continue
            if "##[error]" in line:
                value = line.split("##[error]", 1)[1].strip()
                if value and value not in strong:
                    strong.append(value)
            elif re.search(r"(^|\b)(error|fatal|exception|traceback)(\b|:)", line, re.IGNORECASE):
                if line not in fallback:
                    fallback.append(line)
        selected = strong[:3] or fallback[:2]
        summary = " | ".join(selected)
        return summary[:1000]

    def enrich(self, repository: str, run_id: int) -> tuple[dict[str, str], list[dict[str, Any]]]:
        errors: list[dict[str, Any]] = []
        blank = {"Failed Job": "", "Failed Step": "", "Error Summary": ""}
        try:
            jobs = self._jobs(repository, run_id)
        except Exception as exc:
            message = str(exc)
            if "rate limit" in message.lower() or "API rate limit" in message:
                with self._lock:
                    self._stop_reason = "GitHub Actions enrichment stopped after API rate-limit exhaustion"
            errors.append({"stage": "actions_jobs", "repository": repository, "run_id": run_id, "error": message})
            return blank, errors

        failed_jobs = [job for job in jobs if job.get("conclusion") in FAILURE_LIKE_JOB_CONCLUSIONS]
        if not failed_jobs:
            errors.append({
                "stage": "actions_failed_job",
                "repository": repository,
                "run_id": run_id,
                "error": "jobs response contained no failure-like job",
            })
            return blank, errors

        job_names: list[str] = []
        step_names: list[str] = []
        summaries: list[str] = []
        for job in sorted(failed_jobs, key=lambda item: (str(item.get("name") or ""), int(item.get("id") or 0))):
            job_name = str(job.get("name") or f"job {job.get('id')}")
            job_names.append(job_name)
            failed_steps = [step for step in (job.get("steps") or []) if step.get("conclusion") == "failure"]
            for step in sorted(failed_steps, key=lambda item: (int(item.get("number") or 0), str(item.get("name") or ""))):
                step_names.append(f"{job_name} -> {step.get('name') or 'unnamed step'}")
            if not failed_steps:
                errors.append({
                    "stage": "actions_failed_step",
                    "repository": repository,
                    "run_id": run_id,
                    "job_id": job.get("id"),
                    "error": "failed job contained no step with conclusion=failure",
                })
            summary = ""
            try:
                summary = self._error_from_log(self._job_log(repository, run_id, int(job["id"])))
                if not summary:
                    errors.append({
                        "stage": "actions_job_log_summary",
                        "repository": repository,
                        "run_id": run_id,
                        "job_id": job.get("id"),
                        "error": "job log contained no recognizable error line",
                    })
            except Exception as exc:
                errors.append({
                    "stage": "actions_job_log",
                    "repository": repository,
                    "run_id": run_id,
                    "job_id": job.get("id"),
                    "error": str(exc),
                })
            if not summary:
                try:
                    summary = self._check_error_summary(repository, run_id, job)
                    if not summary:
                        errors.append({
                            "stage": "actions_check_summary",
                            "repository": repository,
                            "run_id": run_id,
                            "job_id": job.get("id"),
                            "error": "check run contained no failure annotation or output summary",
                        })
                except Exception as exc:
                    errors.append({
                        "stage": "actions_check_run",
                        "repository": repository,
                        "run_id": run_id,
                        "job_id": job.get("id"),
                        "error": str(exc),
                    })
            if summary:
                summaries.append(f"{job_name} -> {summary}")
        return {
            "Failed Job": DELIMITER.join(dict.fromkeys(job_names)),
            "Failed Step": DELIMITER.join(dict.fromkeys(step_names)),
            "Error Summary": DELIMITER.join(dict.fromkeys(summaries)),
        }, errors


def _load_inputs(mining: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    episodes = read_jsonl(mining / "episodes" / "all.jsonl")
    comparisons = {item["episode_id"]: item for item in read_jsonl(mining / "diffs" / "comparisons.jsonl")}
    commits = {
        (item["repository"], item["commit_sha"]): item
        for item in read_jsonl(mining / "commit_changes" / "commits.jsonl")
    }
    return episodes, comparisons, commits


def _comparison_file(mining: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        # Stored paths are normally rooted at the workspace (data/mining/...).
        workspace_path = Path.cwd() / path
        mining_relative = mining / path
        path = workspace_path if workspace_path.exists() else mining_relative
    return path


def build(args: argparse.Namespace) -> dict[str, Any]:
    mining = args.input.resolve()
    output = args.output.resolve()
    episodes, comparisons, commits = _load_inputs(mining)
    selected_repository = getattr(args, "repository", None)
    if selected_repository:
        episodes = [e for e in episodes if e["repository"] == selected_repository]
    if not episodes and not selected_repository:
        raise RuntimeError(f"no episodes found under {mining}")

    provider = ComparisonProvider(mining, output, args.offline)
    if getattr(args, "no_local_diff_fallback", False):
        provider.local_fallback = False
    config_patterns = load_config_patterns(getattr(args, "config_files", None))
    config_matcher = lambda path: matches_config_path(path, config_patterns)
    source_patterns = load_source_patterns(getattr(args, "source_files", None))
    source_matcher = lambda path: matches_source_path(path, source_patterns)
    provider.set_commit_records(commits)
    effective_retries = args.retries if args.token else 0
    client = GitHubClient(token=args.token, retries=effective_retries, timeout=args.timeout)
    provider.client = client
    actions = ActionsEnricher(output, client, args.offline or args.skip_actions_enrichment)
    errors: list[dict[str, Any]] = []

    # Failed-run Actions evidence is keyed by repository/run ID and cached once.
    failed_runs: dict[tuple[str, int], dict[str, Any]] = {}
    for episode in episodes:
        for failure in episode["failures"]:
            failed_runs[(episode["repository"], int(failure["run_id"]))] = failure
    action_evidence: dict[tuple[str, int], dict[str, str]] = {}
    workers = max(1, args.workers if args.token else 1)
    LOG.info("Enriching %d unique failed runs with %d worker(s)", len(failed_runs), workers)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(actions.enrich, repository, run_id): (repository, run_id)
            for repository, run_id in sorted(failed_runs)
        }
        done = 0
        for future in as_completed(futures):
            key = futures[future]
            try:
                evidence, run_errors = future.result()
            except Exception as exc:  # Defensive: one run must not abort the derived dataset.
                evidence = {"Failed Job": "", "Failed Step": "", "Error Summary": ""}
                run_errors = [{"stage": "actions_enrichment", "repository": key[0], "run_id": key[1], "error": str(exc)}]
            action_evidence[key] = evidence
            errors.extend(run_errors)
            done += 1
            if done % 50 == 0 or done == len(futures):
                LOG.info("Actions enrichment progress: %d/%d", done, len(futures))

    episode_rows: list[dict[str, Any]] = []
    attempt_rows: list[dict[str, Any]] = []
    comparison_audit: dict[str, dict[str, tuple[str, str] | None]] = {}
    for episode in episodes:
        episode_id = episode["episode_id"]
        repository = episode["repository"]
        failures = episode["failures"]
        previous = episode["previous_pass"]
        recovery = episode["recovery_pass"]
        start_sha = previous.get("head_sha") or ""
        first_sha = failures[0].get("head_sha") or ""
        last_sha = failures[-1].get("head_sha") or ""
        recovery_sha = recovery.get("head_sha") or ""
        comparison = comparisons.get(episode_id)
        full_paths: list[str] | None = None
        repair_paths: list[str] | None = None
        break_paths: list[str] | None = None
        comparison_audit[episode_id] = {
            "complete": (start_sha, recovery_sha),
            "break": (start_sha, first_sha),
            "repair": (last_sha, recovery_sha),
        }

        try:
            full_file = _comparison_file(mining, comparison.get("previous_pass_diff_path") if comparison else None)
            if full_file and full_file.exists():
                full_paths = extract_diff_paths(full_file.read_text(encoding="utf-8", errors="replace"))
            else:
                full_paths = provider.exact_paths(repository, start_sha, recovery_sha)
        except Exception as exc:
            errors.append({
                "stage": "complete_comparison",
                "episode_id": episode_id,
                "repository": repository,
                "base_sha": start_sha,
                "head_sha": recovery_sha,
                "error": str(exc),
            })

        try:
            repair_file = _comparison_file(mining, comparison.get("diff_path") if comparison else None)
            if repair_file and repair_file.exists():
                repair_paths = extract_diff_paths(repair_file.read_text(encoding="utf-8", errors="replace"))
            else:
                repair_paths = provider.exact_paths(repository, last_sha, recovery_sha)
        except Exception as exc:
            errors.append({
                "stage": "repair_comparison",
                "episode_id": episode_id,
                "repository": repository,
                "base_sha": last_sha,
                "head_sha": recovery_sha,
                "error": str(exc),
            })

        try:
            break_paths = provider.exact_paths(repository, start_sha, first_sha)
        except Exception as exc:
            errors.append({
                "stage": "break_comparison",
                "episode_id": episode_id,
                "repository": repository,
                "base_sha": start_sha,
                "head_sha": first_sha,
                "error": str(exc),
            })

        episode_rows.append({
            "Episode ID": episode_id,
            "Repository": repository,
            "GitHub URL": f"https://github.com/{repository}",
            "Workflow Name": episode.get("workflow_name") or "",
            "Branch": episode.get("branch") or "",
            "Start Pass SHA": start_sha,
            "First Fail SHA": first_sha,
            "Fail Shas": DELIMITER.join(str(item.get("head_sha") or "") for item in failures),
            "Failed Attempt Count": len(failures),
            "Recovery Pass SHA": recovery_sha,
            "Time to recovery": _duration(_run_timestamp(failures[0]), _run_timestamp(recovery)),
            "Source files changed": "" if full_paths is None else _classified(full_paths, source_matcher),
            "Config files changed": "" if full_paths is None else _classified(full_paths, config_matcher),
            "Break side config changes": "" if break_paths is None else _classified(break_paths, config_matcher),
            "Repair side config changes": "" if repair_paths is None else _classified(repair_paths, config_matcher),
            "Manual Validation": "",
            "Config Category": "",
            "Notes": "",
        })

        for attempt_number, failure in enumerate(failures, 1):
            sha = failure.get("head_sha") or ""
            commit = commits.get((repository, sha))
            if commit is None:
                attempt_paths: list[str] | None = None
                errors.append({
                    "stage": "attempt_commit_metadata",
                    "episode_id": episode_id,
                    "repository": repository,
                    "run_id": failure.get("run_id"),
                    "sha": sha,
                    "error": "commit metadata is unavailable",
                })
            else:
                attempt_paths = _paths_from_commit(commit)
            evidence = action_evidence.get(
                (repository, int(failure["run_id"])),
                {"Failed Job": "", "Failed Step": "", "Error Summary": ""},
            )
            attempt_rows.append({
                "Episode ID": episode_id,
                "Attempt Number": attempt_number,
                "GitHub Action Run URL": failure.get("html_url") or "",
                "Commit SHA": sha,
                "Timestamp": _run_timestamp(failure),
                "Source files changed": "" if attempt_paths is None else _classified(attempt_paths, source_matcher),
                "Config files changed": "" if attempt_paths is None else _classified(attempt_paths, config_matcher),
                "Patch Type": "",
                "Failed Job": evidence["Failed Job"],
                "Failed Step": evidence["Failed Step"],
                "Error Summary": evidence["Error Summary"],
                "Manual Validation": "",
                "Notes": "",
            })

    _write_csv(output / "episodes.csv", EPISODE_COLUMNS, episode_rows)
    _write_csv(output / "attempts.csv", ATTEMPT_COLUMNS, attempt_rows)
    write_jsonl(output / "extraction_errors.jsonl", errors)

    disk_episode_rows = _read_csv(output / "episodes.csv", EPISODE_COLUMNS)
    disk_attempt_rows = _read_csv(output / "attempts.csv", ATTEMPT_COLUMNS)
    summary = validate_outputs(episodes, disk_episode_rows, disk_attempt_rows, comparison_audit, errors)
    summary["config_patterns"] = list(config_patterns)
    summary["source_patterns"] = list(source_patterns)
    summary["spot_checks"] = _spot_checks(episodes, episode_rows, require_all=not bool(selected_repository))
    _atomic_text(output / "validation_summary.json", json.dumps(summary, indent=2, sort_keys=True) + "\n")
    _atomic_text(
        output / "README.md",
        "# Final analysis dataset\n\n"
        "`episodes.csv` contains one complete PASS -> FAIL+ -> PASS episode per row.\n"
        "`attempts.csv` contains one failed run per row. Multi-value cells use `; ` as the delimiter.\n\n"
        "`Time to recovery` is `recovery_pass.created_at - first_failure.created_at` and uses "
        "`<days>d HH:MM:SS`. Source files are only `.py` and `.pyi`. Config files use only the "
        "patterns specified for this extraction. Empty comparison-derived cells are disambiguated "
        "by `extraction_errors.jsonl`; an empty cell without a corresponding comparison error means "
        "the exact diff was available and contained no matching path.\n\n"
        "Error summaries prefer retained job-log errors, then use Check Run failure annotations, "
        "then Check Run output title/summary/text. GitHub Actions job, log, Check Run, annotation, "
        "and downloaded public exact-diff responses are cached under `cache/`.\n",
    )
    return summary


def _read_csv(path: Path, expected_columns: list[str]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != expected_columns:
            raise AssertionError(f"unexpected CSV columns for {path}: {reader.fieldnames}")
        return list(reader)


def validate_outputs(
    episodes: list[dict[str, Any]],
    episode_rows: list[dict[str, Any]],
    attempt_rows: list[dict[str, Any]],
    comparison_audit: dict[str, dict[str, tuple[str, str] | None]],
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    expected_attempts = sum(len(episode["failures"]) for episode in episodes)
    assert len(episode_rows) == len(episodes)
    assert len(attempt_rows) == expected_attempts
    episode_by_id = {episode["episode_id"]: episode for episode in episodes}
    row_by_id = {row["Episode ID"]: row for row in episode_rows}
    assert len(row_by_id) == len(episodes)
    attempts_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in attempt_rows:
        assert row["Episode ID"] in row_by_id
        attempts_by_episode[row["Episode ID"]].append(row)
        assert row["Patch Type"] == row["Manual Validation"] == row["Notes"] == ""
    for episode_id, episode in episode_by_id.items():
        row = row_by_id[episode_id]
        failures = episode["failures"]
        rows = attempts_by_episode[episode_id]
        assert [int(item["Attempt Number"]) for item in rows] == list(range(1, len(failures) + 1))
        assert int(row["Failed Attempt Count"]) == len(rows)
        assert row["Start Pass SHA"] == episode["previous_pass"].get("head_sha", "")
        assert row["First Fail SHA"] == failures[0].get("head_sha", "")
        assert row["Recovery Pass SHA"] == episode["recovery_pass"].get("head_sha", "")
        assert row["Fail Shas"].split(DELIMITER) == [item.get("head_sha", "") for item in failures]
        assert row["Manual Validation"] == row["Config Category"] == row["Notes"] == ""
        audit = comparison_audit[episode_id]
        assert audit["complete"] == (row["Start Pass SHA"], row["Recovery Pass SHA"])
        assert audit["break"] == (row["Start Pass SHA"], row["First Fail SHA"])
        assert audit["repair"] == (failures[-1].get("head_sha", ""), row["Recovery Pass SHA"])

    error_counts = Counter(item.get("stage", "unknown") for item in errors)
    return {
        "episodes": len(episode_rows),
        "attempts": len(attempt_rows),
        "episodes_with_config_changes": sum(bool(row["Config files changed"]) for row in episode_rows),
        "episodes_with_break_config_changes": sum(bool(row["Break side config changes"]) for row in episode_rows),
        "episodes_with_repair_config_changes": sum(bool(row["Repair side config changes"]) for row in episode_rows),
        "attempts_with_config_changes": sum(bool(row["Config files changed"]) for row in attempt_rows),
        "attempts_with_failed_job": sum(bool(row["Failed Job"]) for row in attempt_rows),
        "attempts_with_failed_step": sum(bool(row["Failed Step"]) for row in attempt_rows),
        "attempts_with_error_summary": sum(bool(row["Error Summary"]) for row in attempt_rows),
        "extraction_errors": len(errors),
        "error_counts_by_stage": dict(sorted(error_counts.items())),
    }


def _spot_checks(episodes: list[dict[str, Any]], rows: list[dict[str, Any]], require_all: bool = True) -> dict[str, str | None]:
    by_id = {row["Episode ID"]: row for row in rows}

    def first(predicate: Any) -> str | None:
        for episode in episodes:
            if predicate(episode, by_id[episode["episode_id"]]):
                return episode["episode_id"]
        return None

    checks = {
        "single_failure": first(lambda episode, row: len(episode["failures"]) == 1),
        "multi_failure": first(lambda episode, row: len(episode["failures"]) > 1),
        "same_sha_recovery": first(lambda episode, row: episode.get("recovery_classification") == "same_sha_recovery"),
        "code_changing_recovery": first(lambda episode, row: episode.get("recovery_classification") == "code_changing_recovery"),
        "config_change": first(lambda episode, row: bool(row["Config files changed"])),
    }
    if require_all and any(value is None for value in checks.values()):
        raise AssertionError(f"unable to select all requested spot checks: {checks}")
    return checks


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Episodes and Attempts research CSVs from existing mining outputs")
    parser.add_argument("--input", type=Path, default=Path("data/mining"))
    parser.add_argument("--output", type=Path, default=Path("final_dataset"))
    parser.add_argument(
        "--config-files",
        type=Path,
        default=None,
        help="Text file containing one repository-relative config glob per line; defaults to the built-in Python list",
    )
    parser.add_argument(
        "--source-files",
        type=Path,
        default=None,
        help="Text file containing one repository-relative source glob per line; defaults to *.py and *.pyi",
    )
    parser.add_argument("--token", default=None, help="GitHub token; defaults to GITHUB_TOKEN/GH_TOKEN")
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--offline", action="store_true", help="Use existing local inputs/caches only")
    parser.add_argument(
        "--skip-actions-enrichment",
        action="store_true",
        help="Do not call Actions jobs/log APIs; exact Git comparisons may still be fetched",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    args.token = args.token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    level = getattr(logging, args.log_level.upper(), None)
    if not isinstance(level, int):
        raise SystemExit(f"invalid log level: {args.log_level}")
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.token and not args.offline:
        LOG.warning("No GitHub token is available; unauthenticated Actions enrichment is rate-limited")
    summary = build(args)
    LOG.info("Completed analysis dataset: %s", json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
