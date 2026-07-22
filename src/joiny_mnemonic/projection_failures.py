from __future__ import annotations

from typing import Any

from .models import MemoryRecord


_FAILURE_OPERATION = "derived_projection_failed"
_RECOVERY_OPERATION = "derived_projection_recovered"


class ProjectionFailureManager:
    """Canonical retry ledger for post-commit derived systems."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.store = service.store
        self.plugins = service.plugins

    @staticmethod
    def _key(system: str, target_kind: str, target_id: str) -> str:
        return f"{system}:{target_kind}:{target_id}"

    def record(
        self,
        *,
        system: str,
        target_kind: str,
        target_id: str,
        branch_id: str,
        error: Exception | str,
    ) -> None:
        key = self._key(system, target_kind, target_id)
        error_type = type(error).__name__ if isinstance(error, Exception) else "Error"
        error_text = str(error)
        self.store.append_internal_events_once(
            f"projection-failure:{key}",
            [{
                "kind": "state",
                "role": None,
                "content": f"derived projection failed: {system} for {target_id}",
                "payload": {
                    "operation": _FAILURE_OPERATION,
                    "failure_key": key,
                    "system": system,
                    "target_kind": target_kind,
                    "target_id": target_id,
                    "error_type": error_type,
                    "error": error_text,
                    "retryable": True,
                },
            }],
            branch_id=branch_id,
        )
        message = f"{system}: {error_text}"
        if message not in self.service.plugin_errors:
            self.service.plugin_errors.append(message)

    def recover(self, failure: dict[str, Any], branch_id: str) -> None:
        key = str(failure["failure_key"])
        self.store.append_internal_events_once(
            f"projection-recovery:{key}",
            [{
                "kind": "state",
                "role": None,
                "content": f"derived projection recovered: {failure['system']}",
                "payload": {
                    "operation": _RECOVERY_OPERATION,
                    "failure_key": key,
                    "system": failure["system"],
                    "target_kind": failure["target_kind"],
                    "target_id": failure["target_id"],
                },
            }],
            branch_id=branch_id,
        )

    def pending(self, *, branch_id: str = "main") -> tuple[dict[str, Any], ...]:
        recovered = {
            str(event.payload.get("failure_key"))
            for event in self.store.events_by_operation(
                _RECOVERY_OPERATION, branch_id=branch_id
            )
        }
        return tuple(
            {
                **event.payload,
                "event_id": event.id,
                "branch_id": event.branch_id,
            }
            for event in self.store.events_by_operation(
                _FAILURE_OPERATION, branch_id=branch_id
            )
            if str(event.payload.get("failure_key")) not in recovered
        )

    def project_memory(self, record: MemoryRecord) -> None:
        projections = (
            ("semantic", self.plugins.semantic, "index"),
            ("knowledge_graph", self.plugins.knowledge_graph, "project"),
        )
        for category, collection, method_name in projections:
            for plugin in collection.values():
                system = f"{category}:{plugin.name}"
                try:
                    getattr(plugin, method_name)(record)
                except Exception as error:
                    self.record(
                        system=system,
                        target_kind="memory",
                        target_id=record.id,
                        branch_id=record.branch_id,
                        error=error,
                    )

    def maintain_event(self, event: Any, *, detached: bool = False) -> tuple[bool, dict[str, Any]]:
        notified = self.service.extraction.notify(detached=detached)
        if self.service.extraction.enabled and not notified:
            self.record(
                system="extraction:wakeup", target_kind="event",
                target_id=event.id, branch_id=event.branch_id,
                error=self.service.extraction.last_wakeup_error or "notification failed",
            )
        witness = self.service.checkpoint_witness()
        if witness.get("status") in {
            "registry_update_failed", "external_witness_unreadable",
        }:
            self.record(
                system="witness:registry", target_kind="event",
                target_id=event.id, branch_id=event.branch_id,
                error=str(witness.get("details", witness)),
            )
        return notified, witness

    def _retry_one(self, failure: dict[str, Any]) -> bool:
        system = str(failure["system"])
        category, _, plugin_name = system.partition(":")
        target_kind = str(failure["target_kind"])
        if category in {"semantic", "knowledge_graph"} and target_kind == "memory":
            collection = (
                self.plugins.semantic
                if category == "semantic"
                else self.plugins.knowledge_graph
            )
            plugin = collection.get(plugin_name)
            if plugin is None:
                return False
            record = self.store.get_memory(str(failure["target_id"]))
            getattr(plugin, "index" if category == "semantic" else "project")(record)
            return True
        if system == "extraction:wakeup":
            return bool(self.service.extraction.notify())
        if system == "witness:registry":
            status = self.service.checkpoint_witness()
            return status.get("status") not in {
                "registry_update_failed",
                "external_witness_unreadable",
            }
        return False

    def retry(self, *, branch_id: str = "main") -> dict[str, int]:
        result = {"pending": 0, "recovered": 0, "failed": 0}
        for failure in self.pending(branch_id=branch_id):
            result["pending"] += 1
            try:
                recovered = self._retry_one(failure)
            except Exception as error:
                message = f"{failure['system']}: {error}"
                if message not in self.service.plugin_errors:
                    self.service.plugin_errors.append(message)
                recovered = False
            if not recovered:
                result["failed"] += 1
                continue
            self.recover(failure, str(failure["branch_id"]))
            result["recovered"] += 1
        return result
