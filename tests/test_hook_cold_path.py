from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)

# The cold-feature invariant (task6.md 6A): a capture-path hook delivery must
# never pay for optional heavyweight machinery. Plugin MODULES may be imported
# (entry-point discovery is cheap and unavoidable); their heavy dependencies
# must stay lazy until a surface that actually needs them runs.
_FORBIDDEN_ON_CAPTURE = ("torch", "sentence_transformers", "transformers")

_PROBE = r"""
import json, sys
from pathlib import Path
from joiny_mnemonic.service import MemoryService
from joiny_mnemonic.hooks import process_hook

root = Path(sys.argv[1])
with MemoryService(root / "cold-path-probe.db", project_root=root) as service:
    process_hook(
        service, "claude-code",
        {
            "hook_event_name": "PostToolUse",
            "session_id": "cold-probe",
            "tool_name": "Bash",
            "tool_input": {"command": "echo probe"},
            "tool_response": "probe done",
        },
    )
print(json.dumps(sorted(name for name in sys.modules)))
"""


class HookColdPathTest(unittest.TestCase):
    def test_capture_hook_imports_no_heavy_optional_dependencies(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-c", _PROBE, str(RUNTIME_ROOT)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        modules = set(json.loads(completed.stdout.strip().splitlines()[-1]))
        offenders = [
            name for name in modules
            if any(
                name == heavy or name.startswith(heavy + ".")
                for heavy in _FORBIDDEN_ON_CAPTURE
            )
        ]
        self.assertEqual(
            offenders, [],
            f"capture-path hook delivery imported heavyweight modules: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
