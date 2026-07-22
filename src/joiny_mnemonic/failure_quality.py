from __future__ import annotations

import re


_LOW_INFORMATION_FAILURE = re.compile(
    r"(?i)^(?:derived failure:\s*)?.+?\s+failed:\s*"
    r"(?:exit code|process exited with code)\s+\d+\s*$"
)


def is_low_information_failure(value: str) -> bool:
    """Return true when a failure contains only a non-zero process exit code."""
    return bool(_LOW_INFORMATION_FAILURE.fullmatch(" ".join(value.split())))
