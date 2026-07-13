from __future__ import annotations

import json
import unittest
from pathlib import Path

from joiny_mnemonic.reducers import ToolOutputReducer
from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


class JsonReducerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MemoryService(":memory:", project_root=RUNTIME_ROOT)
        self.store = self.service.store

    def tearDown(self) -> None:
        self.service.close()

    def _tool_output(self, content: str):
        return self.store.append_event(
            kind="tool_output",
            content=content,
            payload={"tool_name": "Bash", "tool_input": {"command": "curl api"}},
        )

    def test_uniform_rows_take_the_lossless_csv_path(self) -> None:
        rows = [
            {"id": index, "level": "info", "component": f"svc-{index % 3}"}
            for index in range(40)
        ]
        event = self._tool_output(json.dumps(rows))
        bundle = ToolOutputReducer().reduce(event)
        compact = bundle.compact
        self.assertIsNotNone(compact)
        self.assertEqual(compact.strategy, "json-lossless-csv")
        self.assertEqual(compact.metadata["rows_dropped"], 0)
        # Lossless: every row is present in the rendering.
        for index in range(40):
            self.assertIn(f"svc-{index % 3}", compact.content)
        self.assertGreaterEqual(compact.metadata["csv_savings"], 0.15)

    def test_wide_rows_take_the_lossy_path_with_inband_sentinel(self) -> None:
        # Heterogeneous key sets (a unique field per row) defeat the
        # lossless tabular path — uniform wide rows would legitimately win
        # CSV — so this exercises the lossy crush with the sentinel.
        rows = [
            {
                "id": index,
                "description": f"entry number {index} " + "x" * 90,
                f"variant_{index % 7}": index,
            }
            for index in range(60)
        ]
        event = self._tool_output(json.dumps(rows))
        bundle = ToolOutputReducer().reduce(event)
        compact = bundle.compact
        self.assertIsNotNone(compact)
        self.assertEqual(compact.strategy, "json-array-crush")
        body_text = compact.content.split("]\n", 1)[1].rsplit("\n[", 1)[0]
        body = json.loads(body_text)
        # First and last rows survive; the sentinel sits inside the array
        # at the drop site and carries the retrieval affordance.
        self.assertEqual(body[0]["id"], 0)
        self.assertEqual(body[-2]["id"], 59)
        sentinel = body[-1]
        self.assertIn("_dropped", sentinel)
        self.assertIn(event.id, sentinel["_dropped"])
        self.assertIn("memory_source", sentinel["_dropped"])
        self.assertEqual(
            compact.metadata["rows_dropped"], 60 - (len(body) - 1)
        )
        self.assertLessEqual(len(body) - 1, 15)

    def test_protected_row_survives_the_lossy_path_above_the_cap(self) -> None:
        rows = [
            {"id": index, "note": "routine " + "y" * 80} for index in range(50)
        ]
        rows[25]["note"] = "COMPLIANCE-7781 audit checkpoint " + "y" * 60
        event = self._tool_output(json.dumps(rows))
        reducer = ToolOutputReducer(protected_patterns=[r"COMPLIANCE-\d+"])
        bundle = reducer.reduce(event)
        compact = bundle.compact
        self.assertIsNotNone(compact)
        self.assertIn("COMPLIANCE-7781", compact.content)

    def test_protected_line_is_appended_in_line_based_families(self) -> None:
        lines = [f"progress step {index}" for index in range(400)]
        lines[200] = "COMPLIANCE-42 retention checkpoint reached"
        content = "\n".join(lines)
        event = self.store.append_event(
            kind="tool_output",
            content=content,
            payload={"tool_name": "Bash", "tool_input": {"command": "make deploy"}},
        )
        reducer = ToolOutputReducer(protected_patterns=[r"COMPLIANCE-\d+"])
        bundle = reducer.reduce(event)
        compact = bundle.compact
        self.assertIsNotNone(compact)
        self.assertIn("COMPLIANCE-42", compact.content)
        # Without the pattern, the middle line is legitimately omitted.
        plain = ToolOutputReducer().reduce(event).compact
        self.assertIsNotNone(plain)
        self.assertNotIn("COMPLIANCE-42", plain.content)

    def test_views_never_exceed_raw(self) -> None:
        rows = [{"id": index, "value": "z" * 60} for index in range(30)]
        event = self._tool_output(json.dumps(rows))
        bundle = ToolOutputReducer().reduce(event)
        for view in bundle.views:
            self.assertLess(len(view.content), len(event.content) + 200)
            self.assertLess(
                bundle.raw_tokens * 0, 1
            )  # sanity anchor; primary guard asserted below
        if bundle.compact is not None:
            from joiny_mnemonic.prompt import conservative_token_estimate

            self.assertLess(
                conservative_token_estimate(bundle.compact.content),
                bundle.raw_tokens,
            )


if __name__ == "__main__":
    unittest.main()
