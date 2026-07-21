from __future__ import annotations

import base64
import time
import uuid
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, Sequence

from .storage import MemoryStore


def observable_value(value: Any) -> Any:
    """Convert a runtime value to lossless JSON-safe data for the explorer."""
    if is_dataclass(value):
        return observable_value(asdict(value))
    if isinstance(value, Enum):
        return observable_value(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {
            "$type": "bytes",
            "base64": base64.b64encode(value).decode("ascii"),
            "length": len(value),
        }
    if isinstance(value, bytearray):
        return observable_value(bytes(value))
    if isinstance(value, dict):
        return {str(key): observable_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [observable_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"$type": type(value).__name__, "repr": repr(value)}


class DataflowSink(Protocol):
    """Extension point for future OpenTelemetry or external observers."""

    def emit(self, entry: dict[str, Any]) -> None: ...


class NoopDataflowSink:
    def emit(self, entry: dict[str, Any]) -> None:
        return


class DataflowRecorder:
    """Best-effort append-only observer; failures never change core semantics."""

    def __init__(
        self, store: MemoryStore, sinks: Sequence[DataflowSink] = ()
    ) -> None:
        self.store = store
        self.sinks = tuple(sinks)
        self.errors: list[str] = []

    def begin(
        self,
        operation_name: str,
        *,
        source: str,
        branch_id: str = "main",
        session_id: str | None = None,
        parent_operation_id: str | None = None,
        input_value: Any = None,
    ) -> DataflowOperation:
        return DataflowOperation(
            self,
            operation_name=operation_name,
            operation_id=f"op_{uuid.uuid4().hex}",
            parent_operation_id=parent_operation_id,
            source=source,
            branch_id=branch_id,
            session_id=session_id,
            input_value=input_value,
        )

    def emit(self, **values: Any) -> dict[str, Any] | None:
        normalized = {
            key: observable_value(value)
            for key, value in values.items()
            if value is not None
        }
        try:
            entry = self.store.record_dataflow_entry(**normalized)
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")
            return None
        for sink in self.sinks:
            try:
                sink.emit(entry)
            except Exception as exc:
                self.errors.append(f"sink:{type(exc).__name__}: {exc}")
        return entry


class DataflowOperation:
    def __init__(
        self,
        recorder: DataflowRecorder,
        *,
        operation_name: str,
        operation_id: str,
        parent_operation_id: str | None,
        source: str,
        branch_id: str,
        session_id: str | None,
        input_value: Any,
    ) -> None:
        self.recorder = recorder
        self.operation_name = operation_name
        self.operation_id = operation_id
        self.parent_operation_id = parent_operation_id
        self.source = source
        self.branch_id = branch_id
        self.session_id = session_id
        self.started = time.perf_counter()
        self.closed = False
        self._emit(stage="operation", status="started", input_value=input_value)

    def _emit(self, **values: Any) -> dict[str, Any] | None:
        return self.recorder.emit(
            operation_id=self.operation_id,
            parent_operation_id=self.parent_operation_id,
            operation_name=self.operation_name,
            branch_id=self.branch_id,
            session_id=self.session_id,
            source=self.source,
            **values,
        )

    def step(
        self,
        stage: str,
        *,
        input_value: Any = None,
        output_value: Any = None,
        refs: Any = None,
        decision: Any = None,
        status: str = "completed",
        duration_ms: float | None = None,
    ) -> dict[str, Any] | None:
        return self._emit(
            stage=stage,
            status=status,
            input_value=input_value,
            output_value=output_value,
            refs=refs,
            decision=decision,
            duration_ms=duration_ms,
        )

    def complete(
        self, *, output_value: Any = None, refs: Any = None, decision: Any = None
    ) -> None:
        if self.closed:
            return
        self.closed = True
        self._emit(
            stage="operation",
            status="completed",
            output_value=output_value,
            refs=refs,
            decision=decision,
            duration_ms=(time.perf_counter() - self.started) * 1000,
        )

    def skip(self, reason: str, *, output_value: Any = None) -> None:
        if self.closed:
            return
        self.closed = True
        self._emit(
            stage="operation",
            status="skipped",
            output_value=output_value,
            decision={"reason": reason},
            duration_ms=(time.perf_counter() - self.started) * 1000,
        )

    def fail(self, exc: BaseException, *, stage: str = "operation") -> None:
        if self.closed:
            return
        self.closed = True
        self._emit(
            stage=stage,
            status="failed",
            error={"type": type(exc).__name__, "message": str(exc)},
            duration_ms=(time.perf_counter() - self.started) * 1000,
        )
