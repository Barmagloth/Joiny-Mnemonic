from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from joiny_mnemonic.code_index import PythonCodeIndex


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


class CodeIndexTest(unittest.TestCase):
    def test_python_ast_call_graph_context_and_reverse_impact(self) -> None:
        root = RUNTIME_ROOT / f"code-{uuid.uuid4().hex}"
        root.mkdir()
        (root / "sample.py").write_text(
            "def target(value):\n"
            "    return value + 1\n\n"
            "def caller():\n"
            "    return target(1)\n\n"
            "def entrypoint():\n"
            "    return caller()\n",
            encoding="utf-8",
        )
        index = PythonCodeIndex(root)
        report = index.build()
        self.assertEqual(report.files, 1)
        self.assertEqual(report.symbols, 3)
        self.assertEqual(report.unresolved_calls, 0)
        context = index.context("target")
        self.assertIn("def target(value):", context["content"])
        self.assertEqual(context["incoming"][0]["source"], "sample:caller")
        impact = index.impact("target")
        self.assertEqual(
            impact["affected_symbol_ids"],
            ["sample:caller", "sample:entrypoint"],
        )
        self.assertEqual(impact["reverse_callers_by_depth"][0][0]["id"], "sample:caller")

    def test_parse_errors_are_reported_not_hidden(self) -> None:
        root = RUNTIME_ROOT / f"code-{uuid.uuid4().hex}"
        root.mkdir()
        (root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        report = PythonCodeIndex(root).build()
        self.assertEqual(len(report.parse_errors), 1)
        self.assertEqual(report.parse_errors[0]["path"], "broken.py")


if __name__ == "__main__":
    unittest.main()
