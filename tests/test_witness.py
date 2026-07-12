from __future__ import annotations

import copy
import unittest

from joiny_mnemonic.service import MemoryService
from joiny_mnemonic.witness import WitnessRegistry


class MemoryWitness(WitnessRegistry):
    def __init__(self) -> None:
        self.value = None

    def _read(self):
        return copy.deepcopy(self.value)

    def _write(self, value):
        self.value = copy.deepcopy(value)


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
            approval = service.store.append_event(
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


if __name__ == "__main__":
    unittest.main()