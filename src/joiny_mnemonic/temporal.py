"""Temporal logic core.

Single source of temporal truth (task4.md). Pure stdlib, no I/O, no SQL.
Every temporal decision in the product must be expressed through this module;
no other code may compare temporal values directly.

Semantics: half-open intervals over precision envelopes (TSQL2-style
indeterminate instants) evaluated in Kleene three-valued logic. An absent
bound means unknown/open, never an assertion of infinity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo as _TzInfo
from enum import StrEnum


TEMPORAL_PROJECTION_CODE_VERSION = "temporal-core-v1"

PRECISIONS = ("instant", "day", "month", "year", "unknown")


class Truth(StrEnum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


def and3(*values: Truth) -> Truth:
    if any(value is Truth.FALSE for value in values):
        return Truth.FALSE
    if any(value is Truth.UNKNOWN for value in values):
        return Truth.UNKNOWN
    return Truth.TRUE


def or3(*values: Truth) -> Truth:
    if any(value is Truth.TRUE for value in values):
        return Truth.TRUE
    if any(value is Truth.UNKNOWN for value in values):
        return Truth.UNKNOWN
    return Truth.FALSE


def not3(value: Truth) -> Truth:
    if value is Truth.TRUE:
        return Truth.FALSE
    if value is Truth.FALSE:
        return Truth.TRUE
    return Truth.UNKNOWN


class TemporalValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Envelope:
    """The set of instants a bound may denote.

    A singleton envelope is exactly ``lo``. A non-singleton envelope is the
    half-open range ``[lo, hi)``. ``None`` on either side means unbounded on
    that side; the fully unknown bound is ``Envelope(None, None, False)``.
    """

    lo: datetime | None
    hi: datetime | None
    singleton: bool = False

    @property
    def known(self) -> bool:
        return self.singleton or self.lo is not None or self.hi is not None


UNKNOWN_ENVELOPE = Envelope(None, None, False)


def _lt(a: datetime | None, b: datetime | None, *, a_infinite: int, b_infinite: int) -> bool:
    """Compare where ``None`` stands for -inf/+inf depending on the side flag."""
    if a is None and b is None:
        return a_infinite < b_infinite
    if a is None:
        return a_infinite < 0
    if b is None:
        return b_infinite > 0
    return a < b


def _le(a: datetime | None, b: datetime | None, *, a_infinite: int, b_infinite: int) -> bool:
    return not _lt(b, a, a_infinite=b_infinite, b_infinite=a_infinite)


def lt3(a: Envelope, b: Envelope) -> Truth:
    """Three-valued ``every instant of a < every instant of b``."""
    # Definitely: sup(a) below inf(b), respecting attainment.
    if a.singleton and b.singleton:
        definite = _lt(a.lo, b.lo, a_infinite=-1, b_infinite=1)
    elif a.singleton:
        definite = _lt(a.lo, b.lo, a_infinite=-1, b_infinite=-1)
    elif b.singleton:
        definite = _le(a.hi, b.lo, a_infinite=1, b_infinite=-1)
    else:
        definite = _le(a.hi, b.lo, a_infinite=1, b_infinite=-1)
    if definite:
        return Truth.TRUE
    # Possibly: some instant of a below some instant of b.
    if a.singleton and b.singleton:
        possible = _lt(a.lo, b.lo, a_infinite=-1, b_infinite=1)
    elif a.singleton:
        possible = _lt(a.lo, b.hi, a_infinite=-1, b_infinite=1)
    elif b.singleton:
        possible = _lt(a.lo, b.lo, a_infinite=-1, b_infinite=1)
    else:
        possible = _lt(a.lo, b.hi, a_infinite=-1, b_infinite=1)
    return Truth.UNKNOWN if possible else Truth.FALSE


def le3(a: Envelope, b: Envelope) -> Truth:
    """Three-valued ``every instant of a <= every instant of b``."""
    return not3(lt3(b, a))


def eq3(a: Envelope, b: Envelope) -> Truth:
    """Three-valued ``a and b denote the same instant``."""
    if a.singleton and b.singleton:
        return Truth.TRUE if a.lo == b.lo else Truth.FALSE
    # Non-singleton envelopes can only definitely differ (disjoint) or stay unknown.
    if lt3(a, b) is Truth.TRUE or lt3(b, a) is Truth.TRUE:
        return Truth.FALSE
    return Truth.UNKNOWN


@dataclass(frozen=True, slots=True)
class Interval:
    """Half-open validity interval ``[start, end)`` over envelopes."""

    start: Envelope
    end: Envelope


def contains(interval: Interval, point: Envelope) -> Truth:
    return and3(le3(interval.start, point), lt3(point, interval.end))


def overlaps(a: Interval, b: Interval) -> Truth:
    return and3(lt3(a.start, b.end), lt3(b.start, a.end))


def equals(a: Interval, b: Interval) -> Truth:
    return and3(eq3(a.start, b.start), eq3(a.end, b.end))


def precedes(a: Interval, b: Interval) -> Truth:
    return le3(a.end, b.start)


def succeeds(a: Interval, b: Interval) -> Truth:
    return precedes(b, a)


def meets(a: Interval, b: Interval) -> Truth:
    return eq3(a.end, b.start)


VALIDITY_STATUSES = ("current", "current_open", "expired", "not_yet_valid", "unknown")


def validity_status(interval: Interval, now: Envelope) -> str:
    """Fixed composition per task4.md invariant 4."""
    if contains(interval, now) is Truth.TRUE:
        return "current"
    if lt3(now, interval.start) is Truth.TRUE:
        return "not_yet_valid"
    if interval.end.known and le3(interval.end, now) is Truth.TRUE:
        return "expired"
    if not interval.end.known and le3(interval.start, now) is Truth.TRUE:
        return "current_open"
    return "unknown"


def effective_end(interval: Interval, successor_start: Envelope | None) -> Envelope:
    """A successor's start closes the predecessor's open end in projection only.

    The caller must pass only successors visible at the known-at cutoff.
    Stored rows are never modified.
    """
    if successor_start is None or not successor_start.known:
        return interval.end
    if interval.end.known:
        return interval.end
    return successor_start


def possible_conflict(a: Interval, b: Interval) -> Truth:
    """Overlap check for incompatible same-content versions.

    ``UNKNOWN`` is a possible conflict — surfaced, never dropped and never
    asserted as definite.
    """
    return overlaps(a, b)


# --- Normalization -----------------------------------------------------------

_YEAR = re.compile(r"^(\d{4})$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

_RELATIVE_DAYS = {
    "today": 0, "yesterday": -1, "tomorrow": 1,
    "сегодня": 0, "вчера": -1, "завтра": 1,
}


@dataclass(frozen=True, slots=True)
class NormalizedBound:
    value: str | None  # canonical ISO 8601 of the envelope start, tz-aware
    precision: str
    envelope: Envelope


UNKNOWN_BOUND = NormalizedBound(None, "unknown", UNKNOWN_ENVELOPE)


def _require_aware(value: datetime, code: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TemporalValidationError(code, "timestamp must be timezone-aware")
    return value


def _month_add(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + (value.month - 1) + months
    return value.replace(year=month_index // 12, month=month_index % 12 + 1)


def day_envelope(day_start: datetime) -> Envelope:
    return Envelope(day_start, day_start + timedelta(days=1))


def _resolution_tz(anchor: datetime | None) -> _TzInfo:
    """Task4 invariant 6: source-event timezone when recorded, otherwise UTC."""
    if anchor is not None and anchor.tzinfo is not None:
        return anchor.tzinfo
    return UTC


def normalize_bound(
    raw: str | None,
    *,
    anchor: datetime | None = None,
) -> NormalizedBound:
    """Normalize one explicit or relative temporal expression to a bound.

    ``anchor`` is the timezone-aware source-event timestamp; it is required
    only for relative expressions and provides the resolution timezone
    (task4.md invariant 6). Ambiguity raises ``TemporalValidationError`` so
    the caller can quarantine or reject.
    """
    if raw is None:
        return UNKNOWN_BOUND
    text = str(raw).strip()
    if not text:
        return UNKNOWN_BOUND

    lowered = text.casefold()
    if lowered in _RELATIVE_DAYS:
        if anchor is None:
            raise TemporalValidationError(
                "relative_without_anchor",
                "relative expression requires a source-event anchor",
            )
        anchor = _require_aware(anchor, "naive_anchor")
        day_start = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start += timedelta(days=_RELATIVE_DAYS[lowered])
        return NormalizedBound(day_start.isoformat(), "day", day_envelope(day_start))

    zone = _resolution_tz(anchor)
    try:
        match = _YEAR.match(text)
        if match:
            start = datetime(int(match.group(1)), 1, 1, tzinfo=zone)
            return NormalizedBound(
                start.isoformat(), "year", Envelope(start, _month_add(start, 12))
            )
        match = _MONTH.match(text)
        if match:
            start = datetime(int(match.group(1)), int(match.group(2)), 1, tzinfo=zone)
            return NormalizedBound(
                start.isoformat(), "month", Envelope(start, _month_add(start, 1))
            )
        match = _DAY.match(text)
        if match:
            start = datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=zone
            )
            return NormalizedBound(start.isoformat(), "day", day_envelope(start))
    except ValueError as exc:
        raise TemporalValidationError(
            "invalid_calendar_value", f"not a valid calendar date: {text!r}"
        ) from exc

    try:
        instant = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TemporalValidationError(
            "unparseable_temporal", f"unsupported temporal expression: {text!r}"
        ) from exc
    instant = _require_aware(instant, "naive_timestamp")
    return NormalizedBound(instant.isoformat(), "instant", Envelope(instant, instant, True))


def envelope_from_fields(value: str | None, precision: str | None) -> Envelope:
    """Rebuild the envelope of a stored ``(value, precision)`` pair."""
    if value is None:
        return UNKNOWN_ENVELOPE
    kind = (precision or "instant").casefold()
    if kind not in PRECISIONS:
        raise TemporalValidationError("invalid_precision", f"unknown precision: {precision!r}")
    try:
        start = _require_aware(datetime.fromisoformat(value), "naive_stored_bound")
        if kind == "instant":
            return Envelope(start, start, True)
        if kind == "day":
            return day_envelope(start)
        if kind == "month":
            return Envelope(start, _month_add(start, 1))
        if kind == "year":
            return Envelope(start, _month_add(start, 12))
    except TemporalValidationError:
        raise
    except ValueError as exc:
        raise TemporalValidationError(
            "invalid_stored_bound", f"stored bound is not normalizable: {value!r}/{kind}"
        ) from exc
    return UNKNOWN_ENVELOPE


def interval_from_fields(
    valid_from: str | None,
    valid_from_precision: str | None,
    valid_to: str | None,
    valid_to_precision: str | None,
) -> Interval:
    return Interval(
        envelope_from_fields(valid_from, valid_from_precision),
        envelope_from_fields(valid_to, valid_to_precision),
    )


def normalize_interval(
    valid_from: str | None,
    valid_to: str | None,
    *,
    anchor: datetime | None = None,
) -> tuple[NormalizedBound, NormalizedBound]:
    """Normalize and validate a ``[from, to)`` pair; rejects definitely-empty
    or definitely-inverted intervals."""
    start = normalize_bound(valid_from, anchor=anchor)
    end = normalize_bound(valid_to, anchor=anchor)
    if start.envelope.known and end.envelope.known:
        if lt3(end.envelope, start.envelope) is Truth.TRUE:
            raise TemporalValidationError(
                "inverted_interval", "valid_to is definitely before valid_from"
            )
        if (
            start.envelope.singleton
            and end.envelope.singleton
            and start.envelope.lo == end.envelope.lo
        ):
            raise TemporalValidationError("empty_interval", "[t, t) denotes no instant")
    return start, end


def now_envelope(now: datetime) -> Envelope:
    now = _require_aware(now, "naive_now")
    return Envelope(now, now, True)
