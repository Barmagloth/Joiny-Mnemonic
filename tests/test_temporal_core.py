from __future__ import annotations

import itertools
import re
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from joiny_mnemonic import temporal
from joiny_mnemonic.temporal import (
    Envelope,
    Interval,
    Truth,
    and3,
    contains,
    envelope_from_fields,
    eq3,
    equals,
    interval_from_fields,
    le3,
    lt3,
    meets,
    normalize_bound,
    normalize_interval,
    not3,
    now_envelope,
    or3,
    overlaps,
    precedes,
    succeeds,
    validity_status,
)


BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _dt(hours: float) -> datetime:
    return BASE + timedelta(hours=hours)


# Numeric fixture -> (Envelope, oracle sample instants). Pseudo-infinity at ±100
# with interior samples; 0.25 steps distinguish half-open boundaries on the
# integer grid used below.
def _fixture(spec: tuple) -> tuple[str, Envelope, list[float]]:
    kind = spec[0]
    if kind == "unknown":
        return "unknown", Envelope(None, None, False), [-100, -50, -3.5, 0.25, 3.5, 50, 100]
    if kind == "point":
        v = spec[1]
        return f"point({v})", Envelope(_dt(v), _dt(v), True), [v]
    if kind == "range":
        lo, hi = spec[1], spec[2]
        samples = [lo, lo + 0.25, (lo + hi) / 2, hi - 0.25]
        return f"range[{lo},{hi})", Envelope(_dt(lo), _dt(hi), False), samples
    if kind == "from":
        lo = spec[1]
        return f"from[{lo},inf)", Envelope(_dt(lo), None, False), [lo, lo + 0.25, 50, 100]
    if kind == "upto":
        hi = spec[1]
        return f"upto(-inf,{hi})", Envelope(None, _dt(hi), False), [-100, -50, hi - 0.25]
    raise AssertionError(spec)


FIXTURES = [
    _fixture(spec)
    for spec in (
        ("unknown",),
        ("point", 2), ("point", 5), ("point", 8),
        ("range", 2, 5), ("range", 5, 8), ("range", 2, 8), ("range", 4, 6),
        ("from", 5), ("upto", 5),
    )
]


def _oracle_lt(a: list[float], b: list[float]) -> Truth:
    strictly = all(x < y for x in a for y in b)
    possibly = any(x < y for x in a for y in b)
    if strictly:
        return Truth.TRUE
    return Truth.UNKNOWN if possibly else Truth.FALSE


def _oracle_le(a: list[float], b: list[float]) -> Truth:
    strictly = all(x <= y for x in a for y in b)
    possibly = any(x <= y for x in a for y in b)
    if strictly:
        return Truth.TRUE
    return Truth.UNKNOWN if possibly else Truth.FALSE


class TruthTableTest(unittest.TestCase):
    def test_kleene_connectives(self) -> None:
        values = (Truth.TRUE, Truth.FALSE, Truth.UNKNOWN)
        self.assertEqual(and3(), Truth.TRUE)
        self.assertEqual(or3(), Truth.FALSE)
        for a in values:
            self.assertEqual(not3(not3(a)), a)  # involution
            for b in values:
                # De Morgan in Kleene logic.
                self.assertEqual(not3(and3(a, b)), or3(not3(a), not3(b)))
                self.assertEqual(and3(a, b), and3(b, a))
                self.assertEqual(or3(a, b), or3(b, a))

    def test_lt3_le3_exhaustive_against_sampled_oracle(self) -> None:
        for (name_a, env_a, samples_a), (name_b, env_b, samples_b) in itertools.product(
            FIXTURES, FIXTURES
        ):
            with self.subTest(a=name_a, b=name_b):
                self.assertEqual(
                    lt3(env_a, env_b), _oracle_lt(samples_a, samples_b), "lt3 mismatch"
                )
                self.assertEqual(
                    le3(env_a, env_b), _oracle_le(samples_a, samples_b), "le3 mismatch"
                )

    def test_le3_is_dual_of_lt3(self) -> None:
        for (_, env_a, _), (_, env_b, _) in itertools.product(FIXTURES, FIXTURES):
            self.assertEqual(le3(env_a, env_b), not3(lt3(env_b, env_a)))

    def test_eq3_exhaustive(self) -> None:
        for (name_a, env_a, samples_a), (name_b, env_b, samples_b) in itertools.product(
            FIXTURES, FIXTURES
        ):
            with self.subTest(a=name_a, b=name_b):
                result = eq3(env_a, env_b)
                if env_a.singleton and env_b.singleton:
                    expected = Truth.TRUE if samples_a == samples_b else Truth.FALSE
                    self.assertEqual(result, expected)
                elif not (set(_grid(samples_a)) & set(_grid(samples_b))):
                    # Provably disjoint possible-instant sets can never be equal.
                    if lt3(env_a, env_b) is Truth.TRUE or lt3(env_b, env_a) is Truth.TRUE:
                        self.assertEqual(result, Truth.FALSE)
                else:
                    self.assertEqual(result, Truth.UNKNOWN)
                self.assertEqual(result, eq3(env_b, env_a))  # symmetry


def _grid(samples: list[float]) -> list[float]:
    return [round(value * 4) / 4 for value in samples]


def _interval(lo: float | None, hi: float | None, *, singleton_ends: bool = False) -> Interval:
    start = Envelope(_dt(lo), _dt(lo), True) if lo is not None else Envelope(None, None, False)
    end = Envelope(_dt(hi), _dt(hi), True) if hi is not None else Envelope(None, None, False)
    return Interval(start, end)


class IntervalPredicateTest(unittest.TestCase):
    def test_half_open_boundaries(self) -> None:
        interval = _interval(2, 5)
        self.assertEqual(contains(interval, Envelope(_dt(2), _dt(2), True)), Truth.TRUE)
        self.assertEqual(contains(interval, Envelope(_dt(5), _dt(5), True)), Truth.FALSE)
        self.assertEqual(contains(interval, Envelope(_dt(4.999), _dt(4.999), True)), Truth.TRUE)
        self.assertEqual(contains(interval, Envelope(_dt(1.999), _dt(1.999), True)), Truth.FALSE)

    def test_meets_and_precedes_at_shared_boundary(self) -> None:
        a, b = _interval(2, 5), _interval(5, 8)
        self.assertEqual(meets(a, b), Truth.TRUE)
        self.assertEqual(precedes(a, b), Truth.TRUE)  # [2,5) is entirely before [5,8)
        self.assertEqual(overlaps(a, b), Truth.FALSE)
        self.assertEqual(succeeds(b, a), Truth.TRUE)

    def test_predicate_properties_over_fixture_intervals(self) -> None:
        intervals = [
            _interval(2, 5), _interval(5, 8), _interval(2, 8), _interval(4, 6),
            _interval(2, None), _interval(None, 5), _interval(None, None),
            Interval(
                envelope_from_fields(_dt(2).isoformat(), "day"),
                envelope_from_fields(_dt(100).isoformat(), "day"),
            ),
        ]
        for a, b in itertools.product(intervals, intervals):
            self.assertEqual(overlaps(a, b), overlaps(b, a))
            self.assertEqual(precedes(a, b), succeeds(b, a))
            self.assertEqual(equals(a, b), equals(b, a))
            if overlaps(a, b) is Truth.TRUE:
                self.assertNotEqual(precedes(a, b), Truth.TRUE)
                self.assertNotEqual(succeeds(a, b), Truth.TRUE)

    def test_unknown_bounds_never_collapse_to_boolean_truth(self) -> None:
        open_interval = _interval(2, None)
        late_point = Envelope(_dt(50), _dt(50), True)
        # Open end is unknown, not infinity: containment cannot be proven.
        self.assertEqual(contains(open_interval, late_point), Truth.UNKNOWN)
        self.assertEqual(contains(_interval(None, None), late_point), Truth.UNKNOWN)


class ValidityStatusTest(unittest.TestCase):
    NOW = _dt(10)

    def status(self, lo: float | None, hi: float | None) -> str:
        return validity_status(_interval(lo, hi), now_envelope(self.NOW))

    def test_all_branches(self) -> None:
        self.assertEqual(self.status(2, 20), "current")
        self.assertEqual(self.status(2, None), "current_open")
        self.assertEqual(self.status(2, 5), "expired")
        self.assertEqual(self.status(2, 10), "expired")  # [2,10) excludes now=10
        self.assertEqual(self.status(12, 20), "not_yet_valid")
        self.assertEqual(self.status(None, None), "unknown")
        self.assertEqual(self.status(None, 20), "unknown")  # unknown start: not provable
        self.assertEqual(self.status(10, 20), "current")  # inclusive start boundary

    def test_precision_envelope_containing_now_is_unknown_not_current(self) -> None:
        # Start bound known only to day precision, and "now" falls inside that
        # day: the start could still be after now, so nothing is proven.
        day_start = envelope_from_fields(self.NOW.replace(hour=0).isoformat(), "day")
        interval = Interval(day_start, Envelope(None, None, False))
        self.assertEqual(validity_status(interval, now_envelope(self.NOW)), "unknown")

    def test_effective_end_only_closes_open_intervals(self) -> None:
        open_interval = _interval(2, None)
        closed = temporal.effective_end(open_interval, Envelope(_dt(6), _dt(6), True))
        self.assertEqual(closed, Envelope(_dt(6), _dt(6), True))
        explicit = _interval(2, 4)
        kept = temporal.effective_end(explicit, Envelope(_dt(6), _dt(6), True))
        self.assertEqual(kept, explicit.end)
        self.assertEqual(temporal.effective_end(open_interval, None), open_interval.end)


class NormalizationTest(unittest.TestCase):
    def test_precision_detection_and_envelopes(self) -> None:
        anchor = datetime(2026, 7, 13, 12, 0, tzinfo=timezone(timedelta(hours=2)))
        year = normalize_bound("2024", anchor=anchor)
        self.assertEqual(year.precision, "year")
        self.assertEqual(year.envelope.lo, datetime(2024, 1, 1, tzinfo=anchor.tzinfo))
        self.assertEqual(year.envelope.hi, datetime(2025, 1, 1, tzinfo=anchor.tzinfo))
        month = normalize_bound("2024-12", anchor=anchor)
        self.assertEqual(month.precision, "month")
        self.assertEqual(month.envelope.hi, datetime(2025, 1, 1, tzinfo=anchor.tzinfo))
        february = normalize_bound("2024-02", anchor=anchor)
        self.assertEqual(february.envelope.hi, datetime(2024, 3, 1, tzinfo=anchor.tzinfo))
        day = normalize_bound("2024-02-29", anchor=anchor)  # leap day accepted
        self.assertEqual(day.precision, "day")
        instant = normalize_bound("2026-07-13T10:00:00+02:00")
        self.assertEqual(instant.precision, "instant")
        self.assertTrue(instant.envelope.singleton)

    def test_relative_dates_resolve_in_anchor_timezone(self) -> None:
        anchor = datetime(2026, 7, 13, 0, 30, tzinfo=timezone(timedelta(hours=2)))
        yesterday = normalize_bound("вчера", anchor=anchor)
        self.assertEqual(yesterday.precision, "day")
        self.assertEqual(
            yesterday.envelope.lo, datetime(2026, 7, 12, tzinfo=anchor.tzinfo)
        )
        today = normalize_bound("today", anchor=anchor)
        self.assertEqual(today.envelope.lo, datetime(2026, 7, 13, tzinfo=anchor.tzinfo))
        with self.assertRaises(temporal.TemporalValidationError) as ctx:
            normalize_bound("yesterday")
        self.assertEqual(ctx.exception.code, "relative_without_anchor")

    def test_utc_fallback_without_anchor_timezone(self) -> None:
        bound = normalize_bound("2026-07-13")
        self.assertEqual(bound.envelope.lo, datetime(2026, 7, 13, tzinfo=UTC))

    def test_rejections(self) -> None:
        with self.assertRaises(temporal.TemporalValidationError) as naive:
            normalize_bound("2026-07-13T10:00:00")
        self.assertEqual(naive.exception.code, "naive_timestamp")
        with self.assertRaises(temporal.TemporalValidationError) as junk:
            normalize_bound("next sprint")
        self.assertEqual(junk.exception.code, "unparseable_temporal")
        with self.assertRaises(temporal.TemporalValidationError) as inverted:
            normalize_interval("2026-07-13", "2026-07-10")
        self.assertEqual(inverted.exception.code, "inverted_interval")
        with self.assertRaises(temporal.TemporalValidationError) as empty:
            normalize_interval(
                "2026-07-13T10:00:00+00:00", "2026-07-13T10:00:00+00:00"
            )
        self.assertEqual(empty.exception.code, "empty_interval")

    def test_absent_bounds_are_unknown_open(self) -> None:
        start, end = normalize_interval(None, None)
        self.assertIsNone(start.value)
        self.assertEqual(start.precision, "unknown")
        self.assertFalse(end.envelope.known)

    def test_round_trip_through_stored_fields(self) -> None:
        anchor = datetime(2026, 7, 13, 12, 0, tzinfo=timezone(timedelta(hours=2)))
        for text in ("2024", "2024-12", "2024-02-29", "2026-07-13T10:00:00+02:00"):
            bound = normalize_bound(text, anchor=anchor)
            rebuilt = envelope_from_fields(bound.value, bound.precision)
            self.assertEqual(rebuilt, bound.envelope, text)
        rebuilt = interval_from_fields(None, None, None, None)
        self.assertFalse(rebuilt.start.known)
        self.assertFalse(rebuilt.end.known)


class SingleSourceOfTemporalTruthTest(unittest.TestCase):
    def test_no_temporal_comparisons_outside_core(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src" / "joiny_mnemonic"
        field = re.compile(r"\bvalid_(?:from|to)\b")
        offenders = []
        for path in source_root.rglob("*.py"):
            if path.name == "temporal.py":
                continue
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if not field.search(line):
                    continue
                # Any comparison operator on a line touching a valid-time
                # field is treated as a temporal comparison; return-type
                # arrows are not comparisons.
                if re.search(r"[<>]=?", line.replace("->", "")):
                    offenders.append(f"{path}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "temporal comparisons outside temporal.py")


if __name__ == "__main__":
    unittest.main()
