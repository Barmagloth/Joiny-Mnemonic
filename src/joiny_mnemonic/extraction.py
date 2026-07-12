from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .models import Event, ExtractionStatus, MemoryType
from .plugins import Extractor


PARSER_VERSION = "exact-markdown-zones-v1"
POLICY_VERSION = "evidence-bound-auto-v1"
OUTPUT_SCHEMA_VERSION = "memory-candidates-v1"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_content(value: str) -> str:
    return " ".join(value.strip().split())


@dataclass(frozen=True, slots=True)
class ExtractorConfig:
    model_identity: str
    model_version: str
    inference_parameters: Mapping[str, Any]
    prompt_version: str = "joiny-extraction-v1"
    output_schema_version: str = OUTPUT_SCHEMA_VERSION
    parser_version: str = PARSER_VERSION
    evidence_zone_version: str = "markdown-zones-v1"
    validator_policy_version: str = POLICY_VERSION
    context_selection_version: str = "preceding-global-visible-v1"
    normalization_version: str = "unicode-preserving-whitespace-v1"
    context_events: int = 2
    auto_threshold: float = 0.85
    max_retries: int = 3
    worker_concurrency: int = 1

    def descriptor(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def canonical_hash(self) -> str:
        return hashlib.sha256(_canonical_json(self.descriptor()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProposedCandidate:
    memory_type: str
    normalized_content: str
    evidence_quote: str
    confidence: float


@dataclass(frozen=True, slots=True)
class ValidatedCandidate(ProposedCandidate):
    evidence_start: int
    evidence_end: int
    evidence_zone: str
    initial_status: str
    rule_id: str


class ExtractionValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _zone_map(text: str) -> list[str]:
    """Classify every character in one pass; fence, quote and inline code are untrusted."""
    zones = ["prose"] * len(text)
    offset = 0
    fenced = False
    fence_token = ""
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip(" \t")
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if fenced:
            zones[offset : offset + len(line)] = ["fenced_code"] * len(line)
            if (
                marker
                and marker.group(1)[0] == fence_token[0]
                and len(marker.group(1)) >= len(fence_token)
            ):
                fenced = False
                fence_token = ""
            offset += len(line)
            continue
        if marker:
            fenced = True
            fence_token = marker.group(1)
            zones[offset : offset + len(line)] = ["fenced_code"] * len(line)
            offset += len(line)
            continue
        if stripped.startswith(">"):
            zones[offset : offset + len(line)] = ["blockquote"] * len(line)
        else:
            index = 0
            while index < len(line):
                if line[index] != "`":
                    index += 1
                    continue
                width = 1
                while index + width < len(line) and line[index + width] == "`":
                    width += 1
                close = line.find("`" * width, index + width)
                if close < 0:
                    index += width
                    continue
                end = close + width
                zones[offset + index : offset + end] = ["inline_code"] * (end - index)
                index = end
        offset += len(line)
    return zones


def locate_evidence(content: str, quote: str) -> tuple[int, int, str]:
    if not quote:
        raise ExtractionValidationError("empty_evidence", "evidence_quote must be non-empty")
    matches = [match.start() for match in re.finditer(re.escape(quote), content)]
    if not matches:
        raise ExtractionValidationError(
            "evidence_not_found", "quote is not exact canonical evidence"
        )
    if len(matches) != 1:
        raise ExtractionValidationError("ambiguous_evidence", "quote occurs more than once")
    start = matches[0]
    end = start + len(quote)
    quote_zones = set(_zone_map(content)[start:end])
    zone = next(iter(quote_zones)) if len(quote_zones) == 1 else "fenced_code"
    return start, end, zone


def parse_candidates(value: Any) -> tuple[ProposedCandidate, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ExtractionValidationError(
                "malformed_output", "extractor returned invalid JSON"
            ) from exc
    if isinstance(value, Mapping):
        value = value.get("candidates")
    if not isinstance(value, list):
        raise ExtractionValidationError(
            "malformed_output", "output must contain a candidates array"
        )
    allowed = {item.value for item in MemoryType} - {"summary", "index"}
    parsed: list[ProposedCandidate] = []
    for item in value:
        required = {
            "memory_type", "normalized_content", "evidence_quote", "confidence"
        }
        if not isinstance(item, Mapping) or set(item) != required:
            raise ExtractionValidationError(
                "schema_violation", "candidate does not match strict schema"
            )
        memory_type = str(item["memory_type"])
        if memory_type not in allowed:
            raise ExtractionValidationError(
                "unsupported_type", f"unsupported memory type: {memory_type}"
            )
        normalized = normalize_content(str(item["normalized_content"]))
        quote = str(item["evidence_quote"])
        confidence = float(item["confidence"])
        if not normalized:
            raise ExtractionValidationError(
                "empty_content", "normalized content must be non-empty"
            )
        if not 0.0 <= confidence <= 1.0:
            raise ExtractionValidationError(
                "invalid_confidence", "confidence must be between zero and one"
            )
        parsed.append(ProposedCandidate(memory_type, normalized, quote, confidence))
    return tuple(parsed)


def validate_candidate(
    candidate: ProposedCandidate, event: Event, *, threshold: float
) -> ValidatedCandidate:
    start, end, zone = locate_evidence(event.content, candidate.evidence_quote)
    if zone != "prose":
        status, rule = "quarantined", f"untrusted_evidence_zone:{zone}"
    elif candidate.confidence < threshold:
        status, rule = "quarantined", "below_auto_threshold"
    else:
        status, rule = "auto", "exact_prose_above_threshold"
    return ValidatedCandidate(
        **asdict(candidate),
        evidence_start=start,
        evidence_end=end,
        evidence_zone=zone,
        initial_status=status,
        rule_id=rule,
    )


class ExtractionService:
    def __init__(
        self,
        service: Any,
        extractor: Extractor | None,
        config: ExtractorConfig | None,
        *,
        enabled: bool = False,
    ) -> None:
        self.service = service
        self.store = service.store
        self.extractor = extractor
        self.config = config
        self.enabled = bool(enabled and extractor is not None and config is not None)
        if config is not None:
            self.store.register_extractor_config(
                config.canonical_hash, config.descriptor()
            )

    @property
    def config_hash(self) -> str | None:
        return self.config.canonical_hash if self.config else None

    def _context(self, event: Event) -> tuple[Event, ...]:
        if not self.config or self.config.context_events < 1:
            return ()
        return tuple(
            self.store.preceding_canonical_events(
                event.seq, self.config.context_events
            )
        )

    def process_backlog(
        self, *, limit: int | None = None, retry_failed: bool = False
    ) -> dict[str, int]:
        if not self.enabled or self.extractor is None or self.config is None:
            return {"processed": 0, "succeeded": 0, "failed": 0}
        events = self.store.pending_extraction_events(
            self.config.canonical_hash,
            limit=limit,
            retry_failed=retry_failed,
            max_retries=self.config.max_retries,
        )
        result = {"processed": 0, "succeeded": 0, "failed": 0}
        for event in events:
            result["processed"] += 1
            run_id = self.store.ensure_extraction_run(
                event.id, self.config.canonical_hash
            )
            attempt_no, started_at = self.store.start_extraction_attempt(run_id)
            try:
                raw = self.extractor.extract(
                    event,
                    context=self._context(event),
                    config=self.config.descriptor(),
                )
                proposed = parse_candidates(raw)
                valid: list[ValidatedCandidate] = []
                rejected: list[dict[str, Any]] = []
                for candidate in proposed:
                    try:
                        valid.append(
                            validate_candidate(
                                candidate,
                                event,
                                threshold=self.config.auto_threshold,
                            )
                        )
                    except ExtractionValidationError as exc:
                        rejected.append(
                            {
                                "candidate": asdict(candidate),
                                "error_code": exc.code,
                                "redacted_error": str(exc),
                            }
                        )
                self.store.commit_extraction_success(
                    run_id=run_id,
                    attempt_no=attempt_no,
                    started_at=started_at,
                    event=event,
                    candidates=valid,
                    rejections=rejected,
                    raw_response=raw,
                    extractor_config_hash=self.config.canonical_hash,
                )
            except Exception as exc:
                retryable = attempt_no < self.config.max_retries
                self.store.finish_extraction_failure(
                    run_id=run_id,
                    attempt_no=attempt_no,
                    started_at=started_at,
                    outcome=(
                        "retryable_failure" if retryable else "terminal_failure"
                    ),
                    error_code=getattr(exc, "code", type(exc).__name__),
                    redacted_error=str(exc),
                )
                result["failed"] += 1
            else:
                result["succeeded"] += 1
        return result

    def retry_failures(self, *, limit: int | None = None) -> dict[str, int]:
        return self.process_backlog(limit=limit, retry_failed=True)

    def reprocess(
        self, config: ExtractorConfig, *, limit: int | None = None
    ) -> dict[str, int]:
        self.config = config
        self.store.register_extractor_config(
            config.canonical_hash, config.descriptor()
        )
        self.enabled = self.extractor is not None
        return self.process_backlog(limit=limit)

    def status(self) -> ExtractionStatus:
        metrics = self.store.extraction_status(self.config_hash)
        return ExtractionStatus(
            extractor_available=self.extractor is not None,
            extractor_enabled=self.enabled,
            extractor_name=getattr(self.extractor, "name", None),
            extractor_config_hash=self.config_hash,
            **metrics,
        )