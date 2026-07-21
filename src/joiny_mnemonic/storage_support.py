from __future__ import annotations

import functools
from typing import Any


def integrity_checked(method: Any) -> Any:
    @functools.wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        self._guard_read()
        return method(self, *args, **kwargs)

    return wrapped
