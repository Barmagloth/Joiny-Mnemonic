"""Argument parsing and output helpers shared by the command line entry point.

These are separated from ``cli`` so the command surface itself stays within the
module size gate; they carry no command behaviour of their own.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .evaluation import EvaluationTask


def plain(value: Any) -> Any:
    if is_dataclass(value):
        return plain(asdict(value))
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [plain(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def print_json(value: Any) -> None:
    print(json.dumps(plain(value), ensure_ascii=False, indent=2))


def json_object(value: str) -> dict[str, Any]:
    result = json.loads(value)
    if not isinstance(result, dict):
        raise argparse.ArgumentTypeError("expected a JSON object")
    return result


def json_array(value: str) -> list[str]:
    result = json.loads(value)
    if (
        not isinstance(result, list)
        or not result
        or not all(isinstance(item, str) for item in result)
    ):
        raise argparse.ArgumentTypeError("expected a non-empty JSON array of strings")
    return result


def identifier_list(values: list[str]) -> list[str]:
    if len(values) == 1 and values[0].lstrip().startswith("["):
        return json_array(values[0])
    return values


def hook_json_input(stream: Any) -> dict[str, Any]:
    """Read native hook JSON as UTF-8 while accepting an optional UTF-8 BOM."""
    raw_stream = getattr(stream, "buffer", stream)
    source = raw_stream.read()
    if isinstance(source, str):
        source = source.removeprefix("﻿")
    value = json.loads(source)
    if not isinstance(value, dict):
        raise ValueError("hook input must be a JSON object")
    return value


def evaluation_tasks(path: str | Path) -> list[EvaluationTask]:
    values = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(values, list):
        raise ValueError("evaluation task file must contain a JSON array")
    return [
        EvaluationTask(
            id=item["id"],
            query=item.get("query", item.get("task_input", "")),
            required_evidence=tuple(item.get("required_evidence", ())),
            branch_id=item.get("branch_id", "main"),
            task_input=item.get("task_input", item.get("query", "")),
            expected_output=item.get("expected_output"),
            metadata=item.get("metadata"),
        )
        for item in values
    ]
