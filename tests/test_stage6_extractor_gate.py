"""Stage 6 gate: PASSED only for the system that was actually frozen and measured.

JM-INV-007 fails closed in both directions — a report about a different model,
corpus, prompt, schema, threshold set or checker version is refused outright,
and a matching system still has to clear the pre-registered thresholds.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import stage6_extractor_eval as runner  # noqa: E402

from joiny_mnemonic.extraction import ExtractorConfig
from joiny_mnemonic.extractor_backend import EXTRACTION_PROMPT, validate_backend
from joiny_mnemonic.extractor_evaluation_target import (
    CHECKER_VERSION,
    DEFAULT_THRESHOLDS,
    EvaluationIdentityError,
    EvaluationTarget,
    corpus_digest,
    decide,
    evaluate_gate,
    load_target,
    target_mismatches,
    write_latest_pointer,
    write_report,
)


def _backend(**overrides):
    value = {
        "transport": "openai_compatible",
        "endpoint": "http://127.0.0.1:8080/v1",
        "model": "qwen3-4b",
        "revision": "Q4_K_M-2026-07",
        "inference": {"temperature": 0.0},
    }
    value.update(overrides)
    return validate_backend(value)


def _target(backend=None, **overrides):
    backend = backend or _backend()
    values = {
        "extractor_name": "local-llm",
        "extractor_version": "connector-v1",
        "extractor_config_hash": ExtractorConfig.for_backend(backend).canonical_hash,
        "backend": backend.descriptor(),
        "corpus_digests": {"en": "a" * 64, "ru": "b" * 64},
        "scored_types": ("preference",),
        "thresholds": dict(DEFAULT_THRESHOLDS),
        "checker_version": CHECKER_VERSION,
    }
    values.update(overrides)
    return EvaluationTarget(**values)


def _report(tp=18, fp=1, fn=5, false_trusted=0):
    return {
        "by_memory_type": {
            "preference": {
                "true_positive": tp,
                "false_positive": fp,
                "false_negative": fn,
            }
        },
        "false_trusted_records": false_trusted,
    }


class Stage6GateTest(unittest.TestCase):
    def setUp(self):
        self.frozen = _target()
        self.passing = {"en": _report(), "ru": _report()}

    # --- identity -------------------------------------------------------

    def test_matching_system_meeting_thresholds_passes(self):
        decision = decide(self.frozen, _target(), self.passing, worktree_clean=True)
        self.assertTrue(decision["identity_matches"])
        self.assertTrue(decision["passed"])
        self.assertEqual(decision["identity_mismatches"], [])
        self.assertEqual(
            decision["frozen_identity_hash"], decision["actual_identity_hash"]
        )

    def test_different_model_is_refused_even_with_perfect_scores(self):
        other = _target(_backend(model="gemma-3-4b"))
        perfect = {"en": _report(tp=30, fp=0, fn=0), "ru": _report(tp=30, fp=0, fn=0)}
        decision = decide(self.frozen, other, perfect, worktree_clean=True)
        self.assertFalse(decision["passed"])
        self.assertTrue(decision["gate"]["thresholds_met"])
        problems = " ".join(decision["identity_mismatches"])
        self.assertIn("backend", problems)
        self.assertIn("extractor_config_hash", problems)

    def test_changed_corpus_revision_or_checker_is_refused(self):
        for label, other in (
            ("corpus", _target(corpus_digests={"en": "c" * 64, "ru": "b" * 64})),
            ("revision", _target(_backend(revision="Q8_0-2026-07"))),
            ("checker", _target(checker_version="stage6-extractor-gate-v0")),
            ("scope", _target(scored_types=("preference", "fact"))),
            (
                "thresholds",
                _target(thresholds={**DEFAULT_THRESHOLDS, "min_precision": 0.5}),
            ),
        ):
            with self.subTest(changed=label):
                decision = decide(self.frozen, other, self.passing, worktree_clean=True)
                self.assertFalse(decision["passed"])
                self.assertTrue(decision["identity_mismatches"])

    def test_prompt_or_schema_change_moves_the_frozen_config_hash(self):
        # The prompt and schema hashes live inside extractor_config_hash, so a
        # silent edit to either cannot keep an old target valid.
        descriptor = ExtractorConfig.for_backend(_backend()).descriptor()
        self.assertIn("prompt_hash", descriptor)
        self.assertIn("schema_hash", descriptor)
        drifted = _target(extractor_config_hash="0" * 64)
        self.assertTrue(target_mismatches(self.frozen, drifted))

    def test_incomplete_frozen_target_is_refused(self):
        with self.assertRaises(EvaluationIdentityError) as caught:
            EvaluationTarget.from_descriptor({"extractor_name": "local-llm"})
        self.assertEqual(caught.exception.code, "incomplete_target")

    # --- thresholds -----------------------------------------------------

    def test_thresholds_are_applied_per_language(self):
        cases = {
            "precision_below_090": (
                {"en": _report(tp=8, fp=4, fn=1), "ru": _report()},
                "precision_en",
            ),
            "recall_below_070": (
                {"en": _report(tp=5, fp=0, fn=10), "ru": _report()},
                "recall_en",
            ),
        }
        for label, (reports, failing) in cases.items():
            with self.subTest(case=label):
                gate = evaluate_gate(self.frozen, reports)
                self.assertFalse(gate["thresholds_met"])
                self.assertFalse(gate["checks"][failing])

    def test_language_precision_gap_and_false_trusted_fail_closed(self):
        gap = {"en": _report(tp=20, fp=0, fn=2), "ru": _report(tp=18, fp=4, fn=2)}
        self.assertFalse(evaluate_gate(self.frozen, gap)["checks"]["language_precision_gap"])
        trusted = {"en": _report(false_trusted=1), "ru": _report()}
        self.assertFalse(evaluate_gate(self.frozen, trusted)["checks"]["false_trusted"])

    def test_a_type_with_no_predictions_scores_zero_not_one(self):
        empty = {"en": {"by_memory_type": {}, "false_trusted_records": 0}, "ru": _report()}
        gate = evaluate_gate(self.frozen, empty)
        self.assertEqual(gate["rows"]["en"]["precision"], 0.0)
        self.assertFalse(gate["thresholds_met"])

    def test_a_missing_language_report_cannot_be_passed_off_as_a_result(self):
        # A run that reports only the language the model happens to be good at
        # measures a smaller system than the one that was frozen. Dropping the
        # weaker language must be impossible, not merely discouraged.
        perfect_en_only = {"en": _report(tp=30, fp=0, fn=0)}
        with self.assertRaises(EvaluationIdentityError) as caught:
            evaluate_gate(self.frozen, perfect_en_only)
        self.assertEqual(caught.exception.code, "language_coverage_mismatch")
        self.assertIn("ru", str(caught.exception))

    def test_an_unexpected_language_report_is_refused_too(self):
        extra = {"en": _report(), "ru": _report(), "de": _report()}
        with self.assertRaises(EvaluationIdentityError) as caught:
            evaluate_gate(self.frozen, extra)
        self.assertEqual(caught.exception.code, "language_coverage_mismatch")

    # --- provenance -----------------------------------------------------

    def test_a_dirty_worktree_cannot_produce_passed(self):
        # The provenance block already recorded git_dirty; recording is not
        # refusing. A PASSED built from uncommitted code names a system that
        # exists on exactly one machine (JM-INV-007).
        dirty = decide(self.frozen, _target(), self.passing, worktree_clean=False)
        self.assertFalse(dirty["passed"])
        self.assertTrue(dirty["identity_matches"])
        self.assertTrue(dirty["gate"]["thresholds_met"])
        self.assertFalse(dirty["worktree_clean"])

    def test_an_unknown_worktree_state_counts_as_dirty(self):
        unknown = decide(self.frozen, _target(), self.passing, worktree_clean=None)
        self.assertFalse(unknown["passed"])
        self.assertFalse(unknown["worktree_clean"])

    # --- artifacts ------------------------------------------------------

    def test_a_dated_report_is_never_rewritten_with_different_content(self):
        with tempfile.TemporaryDirectory() as directory:
            name = "extractor-eval-20260727-qwen3-4b-abcdef123456.json"
            first = write_report(directory, name, {"report_sha256": "x", "decision": {}})
            self.assertTrue(first.exists())
            # A byte-identical re-run is idempotent.
            write_report(directory, name, {"report_sha256": "x", "decision": {}})
            with self.assertRaises(EvaluationIdentityError) as caught:
                write_report(directory, name, {"report_sha256": "y", "decision": {}})
            self.assertEqual(caught.exception.code, "report_would_be_rewritten")

    def test_latest_is_only_a_pointer_to_a_historical_report(self):
        with tempfile.TemporaryDirectory() as directory:
            name = "extractor-eval-20260727-qwen3-4b-abcdef123456.json"
            body = {
                "report_sha256": "deadbeef",
                "decision": {"actual_identity_hash": "f" * 64, "passed": True},
                "reports": {"en": {"large": "payload"}},
            }
            written = write_report(directory, name, body)
            pointer_path = write_latest_pointer(directory, written)
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            self.assertEqual(pointer["report"], name)
            self.assertEqual(pointer["report_sha256"], "deadbeef")
            self.assertNotIn("reports", pointer)
            self.assertTrue(Path(directory, name).exists())

    def test_target_round_trips_through_a_frozen_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "target.json"
            path.write_text(
                json.dumps(self.frozen.descriptor(), ensure_ascii=False),
                encoding="utf-8",
            )
            self.assertEqual(
                load_target(path).identity_hash, self.frozen.identity_hash
            )

    def test_corpus_digest_tracks_file_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corpus.json"
            path.write_text('{"examples": []}', encoding="utf-8")
            first = corpus_digest(path)
            path.write_text('{"examples": [1]}', encoding="utf-8")
            self.assertNotEqual(first, corpus_digest(path))


class _SilentExtractor:
    """Stands in for a runtime that is reachable but proposes nothing."""

    name = "stub-extractor"

    def extract(self, event, *, context, config):
        return json.dumps({"candidates": []})


class Stage6RunnerWiringTest(unittest.TestCase):
    """The runner is exercised end to end against a stub, never a real model.

    This proves the freeze/measure/publish wiring, not any model's quality: a
    stub that proposes nothing must reach a signed report and a refusal.
    """

    def setUp(self):
        self._load_extractor = runner._load_extractor
        runner._load_extractor = lambda name: _SilentExtractor()
        self.addCleanup(setattr, runner, "_load_extractor", self._load_extractor)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.backend = self.root / "backend.json"
        self.backend.write_text(
            json.dumps(
                {
                    "transport": "openai_compatible",
                    "endpoint": "http://127.0.0.1:8080/v1",
                    "model": "stub-model",
                    "revision": "test",
                }
            ),
            encoding="utf-8",
        )

    def _run(self, *extra):
        argv = sys.argv
        sys.argv = [
            "stage6_extractor_eval.py",
            "--backend",
            str(self.backend),
            "--extractor",
            "stub-extractor",
            "--limit",
            "2",
            "--output-dir",
            str(self.root / "results"),
            *extra,
        ]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return runner.main()
        finally:
            sys.argv = argv

    def test_freeze_then_measure_publishes_a_signed_refusal(self):
        target = self.root / "target.json"
        self.assertEqual(self._run("--freeze", str(target)), 0)
        frozen = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(frozen["backend"]["model"], "stub-model")
        self.assertIn("en", frozen["corpus_digests"])

        self.assertEqual(self._run("--target", str(target)), 1)
        results = self.root / "results"
        reports = [p for p in results.iterdir() if p.name != "latest.json"]
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        self.assertIn("report_sha256", report)
        self.assertTrue(report["candidate_only"])
        self.assertTrue(report["decision"]["identity_matches"])
        self.assertFalse(report["decision"]["passed"])
        self.assertEqual(report["smoke"]["ru"]["examples"], 2)
        pointer = json.loads((results / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["report"], reports[0].name)
        self.assertFalse(pointer["passed"])

    def test_repeating_the_same_configuration_publishes_a_second_report(self):
        # Repeat runs are how stochasticity gets measured. The append-only
        # rule must forbid rewriting a published report, not forbid measuring
        # the same system twice on the same day.
        target = self.root / "target.json"
        self._run("--freeze", str(target))
        self._run("--target", str(target))
        self._run("--target", str(target))
        results = self.root / "results"
        reports = sorted(p for p in results.iterdir() if p.name != "latest.json")
        self.assertEqual(len(reports), 2)
        bodies = [json.loads(p.read_text(encoding="utf-8")) for p in reports]
        self.assertNotEqual(bodies[0]["report_sha256"], bodies[1]["report_sha256"])
        for path, body in zip(reports, bodies):
            self.assertIn(body["report_sha256"][:8], path.name)
            self.assertEqual(
                body["decision"]["actual_identity_hash"],
                bodies[0]["decision"]["actual_identity_hash"],
            )
        pointer = json.loads((results / "latest.json").read_text(encoding="utf-8"))
        self.assertIn(pointer["report"], {p.name for p in reports})

    def test_the_report_carries_the_prompt_it_measured(self):
        target = self.root / "target.json"
        self._run("--freeze", str(target))
        self._run("--target", str(target))
        report = json.loads(
            next(
                p
                for p in (self.root / "results").iterdir()
                if p.name != "latest.json"
            ).read_text(encoding="utf-8")
        )
        # The hash alone cannot be turned back into the question that was
        # asked once the working tree has moved on.
        self.assertEqual(report["prompt_text"], EXTRACTION_PROMPT)

    def test_measuring_a_different_model_than_the_frozen_one_is_refused(self):
        target = self.root / "target.json"
        self._run("--freeze", str(target))
        self.backend.write_text(
            json.dumps(
                {
                    "transport": "openai_compatible",
                    "endpoint": "http://127.0.0.1:8080/v1",
                    "model": "other-model",
                    "revision": "test",
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(self._run("--target", str(target)), 1)
        report = json.loads(
            next(
                p
                for p in (self.root / "results").iterdir()
                if p.name != "latest.json"
            ).read_text(encoding="utf-8")
        )
        self.assertFalse(report["decision"]["identity_matches"])
        self.assertTrue(report["decision"]["identity_mismatches"])

    def test_freeze_and_target_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self._run()


class PublishedDiagnosticsTest(unittest.TestCase):
    """The evidence behind published claims must stay pinned to its run."""

    root = Path(__file__).resolve().parents[1] / "benchmarks/results/stage6"

    def setUp(self):
        manifest = self.root / "diagnostics" / "manifest.json"
        if not manifest.exists():
            self.skipTest("no published diagnostics in this checkout")
        self.manifest = json.loads(manifest.read_text(encoding="utf-8"))

    def test_every_diagnostic_artifact_matches_its_recorded_digest(self):
        import hashlib

        for name, entry in self.manifest["artifacts"].items():
            with self.subTest(artifact=name):
                path = self.root / "diagnostics" / name
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                self.assertEqual(digest, entry["sha256"])

    def test_the_archived_prompt_reproduces_the_identity_it_claims(self):
        # A reverted prompt that cannot be checked against the reports it
        # produced is a story, not an artifact.
        import hashlib

        from joiny_mnemonic import extraction

        archived = (
            self.root / "diagnostics" / "connector-v3-scoped-prompt.txt"
        ).read_text(encoding="utf-8")
        original = extraction.EXTRACTION_PROMPT_HASH
        extraction.EXTRACTION_PROMPT_HASH = hashlib.sha256(
            archived.encode("utf-8")
        ).hexdigest()
        try:
            checked = 0
            for path in self.root.glob("*.json"):
                if path.name == "latest.json":
                    continue
                body = json.loads(path.read_text(encoding="utf-8"))
                target = body["actual_target"]
                if target["extractor_version"] != "connector-v3-scoped":
                    continue
                backend = validate_backend(target["backend"])
                self.assertEqual(
                    ExtractorConfig.for_backend(backend).canonical_hash,
                    target["extractor_config_hash"],
                )
                checked += 1
            self.assertGreater(checked, 0)
        finally:
            extraction.EXTRACTION_PROMPT_HASH = original


if __name__ == "__main__":
    unittest.main()
