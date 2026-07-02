from __future__ import annotations

import unittest


class BulkPassingTests(unittest.TestCase):
    pass


def _passing_case(index: int):
    def test(self: unittest.TestCase) -> None:
        self.assertEqual(index * 2, index + index)
    test.__name__ = f"test_bulk_{index:04d}"
    return test


for _index in range(400):
    setattr(BulkPassingTests, f"test_bulk_{_index:04d}", _passing_case(_index))


class AccountingRegressionTest(unittest.TestCase):
    def test_invoice_total_preserves_decimal_places(self) -> None:
        expected_total = 941.25
        actual_total = 914.25
        self.assertEqual(actual_total, expected_total, "invoice total mismatch at billing.py:417")


if __name__ == "__main__":
    unittest.main(verbosity=2)