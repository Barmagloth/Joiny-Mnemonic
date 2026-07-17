"""Unit tests for the census answer-containment proxy.

Two numeric bugs already slipped through this proxy (bare counts filtered
out of variants; short numerics substring-matched into passage:yes), so
its classification behavior is now pinned (review 2026-07-17).
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

from census import _answer_variants  # noqa: E402


SHORT_NUMERIC = re.compile(r"[\d,.$ ]{1,12}")


def classify(answer: str, haystack: str) -> str:
    """Mirror of the deep-pass classification for a non-empty fragment set."""
    variants = _answer_variants(answer)
    extractive = any(len(v) <= 30 for v in variants)
    if re.fullmatch(SHORT_NUMERIC, " ".join(str(answer).split())):
        return "short_numeric_indeterminate"
    if not extractive:
        return "indeterminate"
    if any(v.casefold() in haystack.casefold() for v in variants):
        return "yes"
    return "no"


class CensusProxyTest(unittest.TestCase):
    def test_short_numerics_are_never_judged(self) -> None:
        # Computed counts, directly-stated values, sums, prices — all
        # unjudgeable by containment (dates collide: '25' in '2023/05/25').
        for answer in ("1", "3", "15", "25", "$50", "$400,000", "3,750"):
            self.assertEqual(
                classify(answer, "on 2023/05/25 I added 25 postcards"),
                "short_numeric_indeterminate",
                answer,
            )

    def test_extractive_short_text_answers_are_judged(self) -> None:
        self.assertEqual(classify("2 AM", "I went to bed at 2 AM"), "yes")
        self.assertEqual(classify("2 AM", "I went to bed early"), "no")
        self.assertEqual(
            classify("3 weeks ago", "that was 3 weeks ago already"), "yes"
        )
        self.assertEqual(classify("Target", "redeemed it at Target"), "yes")
        self.assertEqual(classify("Target", "redeemed it at Walmart"), "no")

    def test_parenthesized_variants_match_either_form(self) -> None:
        answer = "25 minutes and 50 seconds (or 25:50)"
        self.assertEqual(classify(answer, "my best is 25:50 now"), "yes")
        self.assertEqual(
            classify(answer, "my best is 25 minutes and 50 seconds"), "yes"
        )
        self.assertEqual(classify(answer, "my best improved a lot"), "no")

    def test_long_answers_are_indeterminate(self) -> None:
        rubric = (
            "The response should recommend outdoor activities aligned with "
            "the user's love of hiking and photography."
        )
        self.assertEqual(classify(rubric, "anything"), "indeterminate")

    def test_negative_and_decimal_short_numerics(self) -> None:
        # '-3' contains a dash (not in the class) -> falls through to the
        # extractive path and is judged by containment; '3.5' stays
        # numeric-indeterminate. Pinned so future regex edits are deliberate.
        self.assertEqual(classify("3.5", "rated it 3.5 stars"), "short_numeric_indeterminate")
        self.assertEqual(classify("-3", "the delta was -3 points"), "yes")


if __name__ == "__main__":
    unittest.main()
