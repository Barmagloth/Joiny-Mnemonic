from __future__ import annotations

import unittest

from scripts.stage1_gates import complexity_errors, contract_errors


class Stage1GateTest(unittest.TestCase):
    def test_contract_gate_passes_canonical_values(self) -> None:
        self.assertEqual(contract_errors(), [])

    def test_contract_gate_rejects_dead_value_fixture(self) -> None:
        for category in ("origins", "statuses", "modes"):
            with self.subTest(category=category):
                value = f"dead_{category}_fixture"
                errors = contract_errors({category: {value}})
                self.assertTrue(any(value in error for error in errors))

    def test_complexity_gate_uses_frozen_baseline(self) -> None:
        self.assertEqual(complexity_errors(), [])


if __name__ == "__main__":
    unittest.main()
