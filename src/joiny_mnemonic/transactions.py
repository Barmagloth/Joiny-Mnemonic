from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Any


def atomic_service_write(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def wrapped(service: Any, *args: Any, **kwargs: Any) -> Any:
        with service.store._transaction():
            return method(service, *args, **kwargs)

    return wrapped


class TransactionMixin:
    """Re-entrant SQLite transaction boundary with post-commit callbacks."""

    def _init_transactions(self) -> None:
        self._transaction_depth = 0
        self._transaction_failed = False
        self._after_commit_callbacks: list[Callable[[], None]] = []

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        callbacks: tuple[Callable[[], None], ...] = ()
        with self._lock:
            outermost = self._transaction_depth == 0
            if outermost:
                self._conn.execute("BEGIN IMMEDIATE")
                self._transaction_failed = False
                self._after_commit_callbacks.clear()
            self._transaction_depth += 1
            try:
                yield self._conn
            except BaseException:
                self._transaction_failed = True
                if outermost:
                    self._conn.rollback()
                    self._after_commit_callbacks.clear()
                raise
            else:
                if outermost:
                    if self._transaction_failed:
                        self._conn.rollback()
                        self._after_commit_callbacks.clear()
                        raise RuntimeError("nested transaction failed")
                    self._conn.commit()
                    callbacks = tuple(self._after_commit_callbacks)
                    self._after_commit_callbacks.clear()
            finally:
                self._transaction_depth -= 1
                if outermost:
                    self._transaction_failed = False
        for callback in callbacks:
            callback()

    def after_commit(self, callback: Callable[[], None]) -> None:
        if self._transaction_depth:
            self._after_commit_callbacks.append(callback)
        else:
            callback()
