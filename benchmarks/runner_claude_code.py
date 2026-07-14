"""LongMemEval runner bridging the harness to a local Claude Code install.

No API key needed: `claude -p` (headless print mode) authenticates through
the local Claude Code login (e.g. a Max subscription). The harness invokes
this script once per LLM call with JSON on stdin and expects
{"output": "<text>"} on stdout — see src/joiny_mnemonic/longmemeval.py.

Environment knobs:
  LME_MODEL    model alias passed to `claude --model` (default: sonnet)
  LME_CLAUDE   path to the claude executable (default: claude on PATH)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

MODEL = os.environ.get("LME_MODEL", "sonnet")
CLAUDE = os.environ.get("LME_CLAUDE", "claude")

ANSWER_PREAMBLE = (
    "You answer questions about a user's chat history. The [MEMORY PACKET] "
    "below was retrieved from that history; timestamps in [Date: ...] "
    "prefixes and event frames are authoritative. Answer concisely from the "
    "packet. For questions about the user's preferences, habits or tastes, "
    "synthesize a grounded answer from whatever relevant evidence the packet "
    "holds, citing the specifics you drew on — partial evidence deserves a "
    "best-effort answer, not a refusal. For questions that aggregate across "
    "multiple conversations (counts, totals, comparisons, lists, 'how many "
    "times...'), first enumerate every relevant dated entry you can find in "
    "the packet, then derive the answer strictly from that enumeration. Only "
    "when the packet holds nothing relevant at all, say the information is "
    "not available rather than guessing facts.\n\n"
)


def build_prompt(payload: dict) -> str:
    if payload.get("mode") == "judge":
        return payload["prompt"]
    return (
        ANSWER_PREAMBLE
        + f"Current date: {payload.get('question_date', 'unknown')}\n\n"
        + f"[MEMORY PACKET]\n{payload.get('context', '')}\n\n"
        + f"Question: {payload['question']}"
    )


def main() -> int:
    payload = json.loads(sys.stdin.read())
    # Neutral cwd: don't drag project CLAUDE.md / MCP servers into each call.
    workdir = os.path.join(tempfile.gettempdir(), "jm-lme-runner")
    os.makedirs(workdir, exist_ok=True)
    completed = subprocess.run(
        [CLAUDE, "-p", "--model", MODEL],
        input=build_prompt(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=workdir,
        timeout=280,
        shell=(os.name == "nt"),  # claude is a shim script on Windows
    )
    if completed.returncode != 0:
        raise RuntimeError(f"claude -p failed: {completed.stderr[-2000:]}")
    json.dump({"output": completed.stdout.strip()}, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
