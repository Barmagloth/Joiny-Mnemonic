from __future__ import annotations

import copy
import shutil
import unittest
import uuid
from pathlib import Path

from joiny_mnemonic.service import MemoryService
from joiny_mnemonic.witness import WitnessRegistry


class MemoryWitness(WitnessRegistry):
    """In-memory double over the per-project seam (shard layout)."""

    def __init__(self) -> None:
        self.value = None  # dict[project_id, project entry] | None

    def _read_project(self, project_id):
        if self.value is None:
            return None
        return copy.deepcopy(self.value.get(project_id))

    def _write_project(self, project_id, project):
        if self.value is None:
            self.value = {}
        self.value[project_id] = copy.deepcopy(project)


class FakeStore:
    def __init__(self, seq: int, hashes: dict[int, str]) -> None:
        self.seq = seq
        self.hashes = hashes

    def project_identity(self):
        return {
            "project_instance_id": "project_1",
            "chain_id": "chain_1",
            "repository_identity": "repo",
            "canonical_path": "path",
            "bootstrap_hash": "bootstrap",
        }

    def chain_checkpoint(self):
        return {"head_seq": self.seq, "head_hash": self.hashes[self.seq]}

    def chain_hash_at(self, seq):
        return self.hashes.get(seq)


class WitnessTest(unittest.TestCase):
    def test_valid_extension_rollback_and_divergence(self) -> None:
        registry = MemoryWitness()
        initial = FakeStore(2, {1: "h1", 2: "h2"})
        self.assertEqual(
            registry.check_and_update(initial, allow_first=True)["status"],
            "first_checkpoint",
        )
        extension = FakeStore(3, {1: "h1", 2: "h2", 3: "h3"})
        self.assertEqual(
            registry.check_and_update(extension)["status"], "valid_extension"
        )
        rollback = FakeStore(1, {1: "h1"})
        self.assertEqual(
            registry.check_and_update(rollback)["finding"], "history_rollback"
        )
        divergence = FakeStore(3, {1: "h1", 2: "changed", 3: "different"})
        self.assertEqual(
            registry.check_and_update(divergence)["finding"],
            "history_divergence",
        )

    def test_bootstrap_policy_is_immutable_and_rebootstrap_is_sticky(self) -> None:
        with MemoryService(":memory:", project_root=".") as service:
            service.witness = MemoryWitness()
            first = service.initialize_project()
            self.assertTrue(first["initialized"])
            identity = service.store.project_identity()
            self.assertTrue(identity["project_instance_id"].startswith("project_"))
            policy = service.store.active_policy()
            self.assertEqual(policy["origin_evidence_type"], "bootstrap_tofu")
            self.assertFalse(policy["policy"]["automatic_extraction_enabled"])
            requested_policy = service.request_policy_change({"auto_threshold": 0.9})
            self.assertEqual(service.store.active_policy()["version"], 1)
            with self.assertRaises(PermissionError):
                service.store.activate_policy(
                    {"auto_threshold": 0.9},
                    source_event_id=requested_policy.id,
                    origin_evidence_type="extractor",
                )
            approval = service.store.append_host_event(
                adapter="claude",
                kind="message", role="user", content="Подтверждаю новую политику."
            )
            activated = service.store.activate_policy(
                {"auto_threshold": 0.9},
                source_event_id=approval.id,
                origin_evidence_type="host_logical_user",
            )
            self.assertEqual(activated["version"], 2)

            second = service.initialize_project()
            self.assertFalse(second["initialized"])
            findings = service.store.list_security_findings()
            self.assertEqual(findings[0]["finding_type"], "policy_rebootstrapped")
            self.assertEqual(findings[0]["status"], "active")
            requested = service.request_finding_acknowledgement(findings[0]["id"])
            self.assertTrue(requested["event_id"].startswith("evt_"))
            findings = service.store.list_security_findings()
            self.assertEqual(findings[0]["status"], "acknowledgement_requested")
            with self.assertRaises(PermissionError):
                service.store.transition_finding(
                    findings[0]["id"],
                    "acknowledged",
                    source_event_id=requested["event_id"],
                    actor="tool",
                    origin_evidence_type="extractor",
                )
            service.acknowledge_finding_from_user(
                findings[0]["id"], source_event_id=approval.id
            )
            self.assertTrue(service.store.list_security_findings()[0]["acknowledged"])


    def test_missing_registry_after_checkpoint_is_reported(self) -> None:
        registry = MemoryWitness()
        store = FakeStore(1, {1: "h1"})
        self.assertEqual(
            registry.check_and_update(store, allow_first=True)["status"],
            "first_checkpoint",
        )
        registry.value = None
        result = registry.check_and_update(store)
        self.assertEqual(result["status"], "external_witness_missing")
        self.assertEqual(result["finding"], "external_witness_missing")

    def test_shard_layout_hot_path_and_legacy_migration(self) -> None:
        """Packet-assembly fix: the hot path touches only the project's own
        shard; witnessed heads migrate read-only from the legacy monolith."""
        runtime = Path(__file__).resolve().parent / "runtime"
        runtime.mkdir(exist_ok=True)
        suffix = uuid.uuid4().hex
        registry_path = runtime / f"witnesses-shard-{suffix}.json"
        try:
            witness = WitnessRegistry(registry_path)
            store = FakeStore(2, {1: "h1", 2: "h2"})
            self.assertEqual(
                witness.check_and_update(store, allow_first=True)["status"],
                "first_checkpoint",
            )
            shard = witness._shard_path("project_1")
            self.assertTrue(shard.exists())
            # The monolith is never created or rewritten by the hot path.
            self.assertFalse(registry_path.exists())
            self.assertEqual(
                witness.check_and_update(FakeStore(3, {1: "h1", 2: "h2", 3: "h3"}))[
                    "status"
                ],
                "valid_extension",
            )
            # Rollback detection works from the shard alone.
            self.assertEqual(
                witness.check_and_update(FakeStore(1, {1: "h1"}))["finding"],
                "history_rollback",
            )

            # Legacy migration: a monolith-only project keeps its witnessed
            # head — a rollback below it is detected on the first sharded
            # check, and the entry lands in a shard afterwards.
            legacy_only = WitnessRegistry(
                runtime / f"witnesses-legacy-{suffix}.json"
            )
            legacy_only._write(
                {
                    "version": 1,
                    "projects": {
                        "project_1": {
                            "repository_identity": "repo",
                            "canonical_path": "path",
                            "bootstrap_hash": "bootstrap",
                            "first_seen_at": "2026-01-01T00:00:00+00:00",
                            "chains": {
                                "chain_1": {
                                    "project_instance_id": "project_1",
                                    "chain_id": "chain_1",
                                    "head_seq": 5,
                                    "head_hash": "h5",
                                    "bootstrap_hash": "bootstrap",
                                    "first_seen_at": "2026-01-01T00:00:00+00:00",
                                    "last_seen_at": "2026-01-01T00:00:00+00:00",
                                }
                            },
                        }
                    },
                }
            )
            self.assertEqual(
                legacy_only.check_and_update(FakeStore(2, {1: "h1", 2: "h2"}))[
                    "finding"
                ],
                "history_rollback",
            )
            extended = legacy_only.check_and_update(
                FakeStore(6, {5: "h5", 6: "h6"})
            )
            self.assertEqual(extended["status"], "valid_extension")
            migrated = legacy_only._shard_path("project_1")
            self.assertTrue(migrated.exists())
            # After migration the monolith is dead weight: removing it must
            # not lose the witnessed head.
            legacy_only.path.unlink()
            self.assertEqual(
                legacy_only.check_and_update(FakeStore(4, {4: "h4"}))["finding"],
                "history_rollback",
            )
        finally:
            import shutil as _shutil

            registry_path.unlink(missing_ok=True)
            for candidate in (
                registry_path.with_suffix(".d"),
                (runtime / f"witnesses-legacy-{suffix}.json").with_suffix(".d"),
            ):
                _shutil.rmtree(candidate, ignore_errors=True)
            (runtime / f"witnesses-legacy-{suffix}.json").unlink(missing_ok=True)

    def test_environment_variable_sets_default_registry_path(self) -> None:
        import os

        runtime = Path(__file__).resolve().parent / "runtime"
        runtime.mkdir(exist_ok=True)
        target = runtime / f"witnesses-env-{uuid.uuid4().hex}.json"
        previous = os.environ.get("JOINY_MNEMONIC_WITNESS_REGISTRY")
        os.environ["JOINY_MNEMONIC_WITNESS_REGISTRY"] = str(target)
        try:
            witness = WitnessRegistry()
            self.assertEqual(witness.path, target.resolve())
            explicit = WitnessRegistry(runtime / "explicit.json")
            self.assertEqual(explicit.path.name, "explicit.json")
        finally:
            if previous is None:
                os.environ.pop("JOINY_MNEMONIC_WITNESS_REGISTRY", None)
            else:
                os.environ["JOINY_MNEMONIC_WITNESS_REGISTRY"] = previous

    def test_known_project_database_missing_sees_shards(self) -> None:
        runtime = Path(__file__).resolve().parent / "runtime"
        runtime.mkdir(exist_ok=True)
        suffix = uuid.uuid4().hex
        project = runtime / f"witness-shard-project-{suffix}"
        registry_path = runtime / f"witnesses-scan-{suffix}.json"
        project.mkdir()
        try:
            witness = WitnessRegistry(registry_path)
            witness._write_project(
                "project_shard",
                {
                    "project_instance_id": "project_shard",
                    "canonical_path": str(project.resolve()),
                    "chains": {},
                },
            )
            findings = witness.known_project_database_missing(project)
            self.assertEqual(len(findings), 1)
            self.assertEqual(
                findings[0]["project_instance_id"], "project_shard"
            )
        finally:
            shutil.rmtree(project, ignore_errors=True)
            shutil.rmtree(registry_path.with_suffix(".d"), ignore_errors=True)
            registry_path.unlink(missing_ok=True)

    def test_known_project_database_disappearance_is_reported(self) -> None:
        runtime = Path(__file__).resolve().parent / "runtime"
        runtime.mkdir(exist_ok=True)
        suffix = uuid.uuid4().hex
        project = runtime / f"witness-project-{suffix}"
        witness_path = runtime / f"witnesses-{suffix}.json"
        project.mkdir()
        try:
            witness = WitnessRegistry(witness_path)
            witness._write(
                {
                    "version": 1,
                    "projects": {
                        "project_missing": {
                            "canonical_path": str(project.resolve()),
                            "chains": {},
                        }
                    },
                }
            )
            findings = witness.known_project_database_missing(project)
            self.assertEqual(len(findings), 1)
            self.assertEqual(
                findings[0]["finding"], "known_project_database_missing"
            )
            database = project / ".joiny-mnemonic" / "memory.db"
            database.parent.mkdir()
            database.touch()
            self.assertEqual(witness.known_project_database_missing(project), ())
        finally:
            shutil.rmtree(project)
            witness_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
