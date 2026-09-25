"""One-command GitHub Actions mining and final analysis-dataset generation."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from .build_analysis_dataset import build as build_analysis


LOG = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mine GitHub Actions episodes and create final Episodes/Attempts CSVs"
    )
    parser.add_argument("--repos", type=Path, required=True, help="Text file with one owner/repository per line")
    parser.add_argument(
        "--config-files",
        type=Path,
        default=None,
        help="Text file with one repository-relative config glob per line; defaults to the built-in Python list",
    )
    parser.add_argument(
        "--source-files",
        type=Path,
        default=None,
        help="Text file with one repository-relative source glob per line",
    )
    parser.add_argument("--mining-output", type=Path, default=Path("data/mining"))
    parser.add_argument("--output", type=Path, default=Path("final_dataset"))
    parser.add_argument(
        "--episodes-input",
        type=Path,
        default=None,
        help="Optional filtered episode JSONL; valid with --analysis-only or --offline",
    )
    parser.add_argument("--token", default=None, help="GitHub token; defaults to GITHUB_TOKEN/GH_TOKEN")
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--since", default=None, help="Inclusive UTC Actions-run collection start")
    parser.add_argument("--until", default=None, help="Inclusive UTC Actions-run collection cutoff")
    parser.add_argument("--refresh", action="store_true", help="Ignore complete raw-run checkpoints")
    parser.add_argument("--retry-extraction-errors", action="store_true",
                        help="Rebuild per-repository final results, reusing run and evidence caches")
    parser.add_argument("--no-local-diff-fallback", action="store_true")
    parser.add_argument("--skip-actions-enrichment", action="store_true")
    parser.add_argument(
        "--analysis-only",
        action="store_true",
        help="Skip mining and rebuild CSVs from an existing --mining-output dataset",
    )
    parser.add_argument("--offline", action="store_true", help="Use only existing local data/caches (implies --analysis-only)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    args.token = args.token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    return args


def run(args: argparse.Namespace) -> int:
    if not (args.analysis_only or args.offline):
        if args.episodes_input:
            raise ValueError("--episodes-input requires --analysis-only or --offline")
        from .repository_workflow import run_repositories
        return run_repositories(args)
    summary = build_analysis(argparse.Namespace(
        input=args.mining_output,
        output=args.output,
        episodes_input=args.episodes_input,
        config_files=args.config_files,
        source_files=args.source_files,
        token=args.token,
        retries=args.retries,
        timeout=args.timeout,
        workers=args.workers,
        offline=args.offline,
        skip_actions_enrichment=args.skip_actions_enrichment,
        no_local_diff_fallback=args.no_local_diff_fallback,
    ))
    LOG.info("Final dataset summary: %s", json.dumps(summary, sort_keys=True))
    return 1 if summary.get("extraction_errors") else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    level = getattr(logging, args.log_level.upper(), None)
    if not isinstance(level, int):
        raise SystemExit(f"invalid log level: {args.log_level}")
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.token and not args.offline:
        LOG.warning("No GitHub token is available; unauthenticated requests are heavily rate-limited")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
