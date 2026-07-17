"""Extraction gate: evaluate an LLM extractor against the v2 corpora.

System under test: a bridge extractor calling the local Claude Code CLI
(same model family as the keyed distiller — the gate target agreed
2026-07-17: the shipped extraction path must approach the probe
distiller). The corpus stays hand-labelled with no LLM in the loop; the
LLM is only the system being measured.

Gate (pre-registered): per consumed type (preference first)
precision >= 0.9 AND recall >= 0.7, false_trusted_records == 0, on both
languages with no per-language precision gap > 10pp.

Usage:
  python benchmarks/extraction_gate.py [--limit N] [--model haiku]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from joiny_mnemonic.extraction import ExtractorConfig  # noqa: E402
from joiny_mnemonic.extraction_evaluation import evaluate_extractor  # noqa: E402
from joiny_mnemonic.report_signing import stamp_report  # noqa: E402


EXTRACT_PROMPT = """You extract durable memory candidates from ONE user message.

Types: fact, decision, task, preference, failure, lesson. Extract ONLY
statements that are durable (worth remembering across sessions) and
asserted by the author about themselves or their project. Do NOT extract:
other people's tastes, hypotheticals, questions, sarcasm, transient
one-off wishes, quoted third parties, fiction, or explicitly past phases.

A "preference" is the author's own standing taste, habit or standing
instruction about how to serve them.

Rules for each candidate:
- "evidence_quote": an EXACT contiguous substring of the message (copy it
  byte for byte, do not paraphrase, do not add or remove characters). It
  must occur exactly once in the message. Quote the minimal span that
  carries the assertion. Quoting from code spans, fenced blocks or
  quoted lines is allowed but such candidates are untrusted.
- "normalized_content": the evidence quote rebased to a standalone
  assertion: drop first-person scaffolding (I/my/we decided to...),
  keep the wording otherwise, capitalize the first letter, end with a
  period. Keep the language of the original message.
- "confidence": your confidence 0..1 that this is a durable candidate of
  that type.
- Output STRICT JSON only: {{"candidates": [{{"memory_type": ...,
  "normalized_content": ..., "evidence_quote": ..., "confidence": ...}}]}}
  with EXACTLY those four keys per candidate. Empty list if nothing
  qualifies. No prose, no markdown fences.

Preceding context (may resolve pronouns, do not quote from it):
{context}

Message:
{message}"""


class ClaudeCodeExtractor:
    name = "claude-code-bridge"
    model_identity = "claude-code-cli"
    model_version = "bridge-v1"
    inference_parameters: dict = {}

    def __init__(self, model: str) -> None:
        self.model = model
        self.workdir = os.path.join(tempfile.gettempdir(), "jm-extraction-gate")
        os.makedirs(self.workdir, exist_ok=True)
        self._memo: dict[str, str] = {}

    def extract(self, event, *, context=(), config=None) -> str:
        prompt = EXTRACT_PROMPT.format(
            context="\n".join(item.content for item in context) or "(none)",
            message=event.content,
        )
        if prompt in self._memo:
            return self._memo[prompt]
        completed = subprocess.run(
            ["claude", "-p", "--model", self.model],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=self.workdir,
            timeout=280,
            shell=(os.name == "nt"),
        )
        if completed.returncode != 0:
            raise RuntimeError(f"claude -p failed: {completed.stderr[-500:]}")
        output = completed.stdout.strip()
        # Tolerate accidental fences; the payload must still be strict JSON.
        start = output.find("{")
        end = output.rfind("}")
        result = output[start : end + 1] if start >= 0 and end > start else output
        self._memo[prompt] = result
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--limit", type=int, default=0, help="first N examples per corpus (smoke)")
    parser.add_argument("--output-dir", default="benchmarks/results")
    args = parser.parse_args()

    extractor = ClaudeCodeExtractor(args.model)
    config = ExtractorConfig(
        model_identity=f"claude-code:{args.model}",
        model_version="bridge-v1",
        inference_parameters={},
    )
    reports: dict[str, dict] = {}
    exact_reports: dict[str, dict] = {}
    for corpus_name in ("extraction_en_v2", "extraction_ru_v2"):
        corpus_path = ROOT / "evals" / f"{corpus_name}.json"
        if args.limit:
            trimmed = json.loads(corpus_path.read_text(encoding="utf-8"))
            trimmed["examples"] = trimmed["examples"][: args.limit]
            tmp = Path(tempfile.gettempdir()) / f"{corpus_name}-limit{args.limit}.json"
            tmp.write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8")
            corpus_path = tmp
        # Gate mode measures typing quality (type + overlapping span); the
        # exact-triple pass reuses memoized LLM outputs, so it is free and
        # keeps the provenance-calligraphy number visible alongside.
        report = evaluate_extractor(
            extractor, config, corpus_path, match_mode="type-span"
        )
        exact_reports[corpus_name] = evaluate_extractor(
            extractor, config, corpus_path, match_mode="exact-triple"
        )
        reports[corpus_name] = report
        print(f"[{corpus_name}] overall={report['overall']}", flush=True)
        print(
            f"[{corpus_name}] preference="
            f"{report['by_memory_type'].get('preference')}",
            flush=True,
        )
        print(
            f"[{corpus_name}] false_trusted={report['false_trusted_records']} "
            f"quarantine_rate={report['quarantine_rate']:.3f} "
            f"exact_acceptance={report['exact_evidence_acceptance_rate']:.3f}",
            flush=True,
        )

    def gate_row(report: dict) -> dict:
        pref = report["by_memory_type"].get(
            "preference", {"precision": 0.0, "recall": 0.0}
        )
        return {
            "preference_precision": pref["precision"],
            "preference_recall": pref["recall"],
            "false_trusted": report["false_trusted_records"],
        }

    en, ru = gate_row(reports["extraction_en_v2"]), gate_row(reports["extraction_ru_v2"])
    gate = {
        "precision_en_ge_0.9": en["preference_precision"] >= 0.9,
        "precision_ru_ge_0.9": ru["preference_precision"] >= 0.9,
        "recall_en_ge_0.7": en["preference_recall"] >= 0.7,
        "recall_ru_ge_0.7": ru["preference_recall"] >= 0.7,
        "false_trusted_zero": en["false_trusted"] + ru["false_trusted"] == 0,
        "language_gap_le_10pp": abs(
            en["preference_precision"] - ru["preference_precision"]
        ) <= 0.10,
    }
    combined = {
        "schema": "joiny-mnemonic-extraction-gate-v1",
        "system_under_test": {
            "extractor": extractor.name, "model": args.model,
            "prompt": "extraction-gate-bridge-v1",
        },
        "corpora": {name: r["corpus_version"] for name, r in reports.items()},
        "gate_match_mode": "type-span",
        "reports": reports,
        "exact_triple_reports": exact_reports,
        "gate_rows": {"en": en, "ru": ru},
        "gate": gate,
        "passed": all(gate.values()),
        "limited_to": args.limit or None,
    }
    combined = stamp_report(combined, repo_root=ROOT)
    output_dir = ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"-limit{args.limit}" if args.limit else ""
    out = output_dir / f"extraction-gate-latest{suffix}.json"
    out.write_text(
        json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"passed": combined["passed"], "gate": gate}, indent=1))
    return 0 if combined["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
