from __future__ import annotations

from typing import Any


RESULT_MAP = {
    "success": "PASS",
    "failure": "FAIL",
    "cancelled": "CANCELLED",
    "skipped": "SKIPPED",
}


def normalize_run(run: dict[str, Any]) -> dict[str, Any]:
    result = RESULT_MAP.get(run.get("conclusion"), run.get("conclusion") or "INCOMPLETE")
    return {**run, "normalized_result": result}

