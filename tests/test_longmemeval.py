from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path

from joiny_mnemonic.longmemeval import (
    LMEHarness,
    SubprocessLLMRunner,
    judge_prompt_for,
    load_dataset,
    main,
    parse_boxed,
)


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


FAKE_RUNNER = '''
import json, sys
request = json.load(sys.stdin)
if request["mode"] == "answer":
    context = request["context"]
    marker = None
    for token in context.split():
        if token.startswith("SECRET-"):
            marker = token.strip(".,")
            break
    output = marker if marker else "I don't know; the information is not available."
else:
    prompt = request["prompt"]
    if "unanswerable" in prompt:
        verdict = "yes" if "don't know" in prompt else "no"
        output = "ABSTAIN-PATH \\\\boxed{" + verdict + "}"
    else:
        answer = prompt.split("Correct Answer: ")[1].split("\\n")[0].strip()
        response = prompt.split("Model Response: ")[1].split("\\n")[0]
        verdict = "yes" if answer in response else "no"
        output = "reasoning... \\\\boxed{" + verdict + "}"
print(json.dumps({"output": output}))
'''


def _dataset() -> list[dict]:
    return [
        {
            "question_id": "q-1",
            "question_type": "single-session-user",
            "question": "What is the launch code word?",
            "answer": "SECRET-42",
            "question_date": "2023-06-01",
            "haystack_session_ids": ["s1", "s2"],
            "haystack_dates": ["2023-05-20", "2023-05-25"],
            "haystack_sessions": [
                [
                    {"role": "user", "content": "We picked the launch code word SECRET-42 yesterday."},
                    {"role": "assistant", "content": "Noted."},
                ],
                [
                    {"role": "user", "content": "Weather is nice."},
                    {"role": "assistant", "content": "Indeed."},
                ],
            ],
        },
        {
            "question_id": "q-2_abs",
            "question_type": "single-session-user",
            "question": "What is my cat's name?",
            "answer": "The cat is never mentioned.",
            "question_date": "2023-06-01",
            "haystack_session_ids": ["s1"],
            "haystack_dates": ["2023-05-20"],
            "haystack_sessions": [
                [{"role": "user", "content": "I own a very old bicycle."}]
            ],
        },
    ]


class LongMemEvalHarnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = RUNTIME_ROOT / f"lme-{uuid.uuid4().hex}"
        self.root.mkdir()
        self.dataset_path = self.root / "dataset.json"
        self.dataset_path.write_text(json.dumps(_dataset()), encoding="utf-8")
        self.runner_path = self.root / "fake_runner.py"
        self.runner_path.write_text(FAKE_RUNNER, encoding="utf-8")

    def test_parse_boxed(self) -> None:
        self.assertTrue(parse_boxed("blah \\boxed{yes}"))
        self.assertFalse(parse_boxed("first \\boxed{yes} then \\boxed{no}"))
        self.assertIsNone(parse_boxed("no verdict here"))

    def test_judge_prompt_routing(self) -> None:
        self.assertIn("unanswerable", judge_prompt_for("x_abs", "single-session-user"))
        self.assertIn("Rubric", judge_prompt_for("x", "single-session-preference"))
        self.assertIn("off-by-one", judge_prompt_for("x", "temporal-reasoning"))
        self.assertIn("updated answer", judge_prompt_for("x", "knowledge-update"))

    def test_end_to_end_with_deterministic_runner(self) -> None:
        items = load_dataset(self.dataset_path)
        harness = LMEHarness(
            SubprocessLLMRunner([sys.executable, str(self.runner_path)]),
            context_budget_tokens=2048,
        )
        for item in items:
            harness.run_question(item)
        report = harness.report()
        # q-1: retrieval must surface the SECRET token; q-2_abs: abstention.
        self.assertEqual(
            report["overall"],
            {"total": 2, "correct": 2, "accuracy": 1.0, "ci95": 0.0},
        )
        self.assertEqual(report["unparseable_judgments"], 0)
        abstention = next(
            record for record in harness.results
            if record["question_id"] == "q-2_abs"
        )
        self.assertIn("ABSTAIN-PATH", abstention["judge_output"])
        answered = next(
            record for record in harness.results if record["question_id"] == "q-1"
        )
        self.assertIn("SECRET-42", answered["answer"])
        self.assertTrue(answered["retrieved_ids"])

    def test_cli_writes_report_files(self) -> None:
        output_dir = self.root / "results"
        code = main(
            [
                str(self.dataset_path),
                "--runner-command",
                json.dumps([sys.executable, str(self.runner_path)]),
                "--output-dir", str(output_dir),
            ]
        )
        self.assertEqual(code, 0)
        report = json.loads(
            (output_dir / "longmemeval-latest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(report["overall"]["accuracy"], 1.0)
        self.assertIn("dataset_sha256_16", report)
        lines = (
            (output_dir / "longmemeval-latest.jsonl")
            .read_text(encoding="utf-8").strip().splitlines()
        )
        self.assertEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()
