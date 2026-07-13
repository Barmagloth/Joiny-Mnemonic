"""LongMemEval runner bridging joiny-mnemonic-longmemeval to the Claude API.

The harness (src/joiny_mnemonic/longmemeval.py) invokes this script once per
LLM call with a JSON request on stdin:

  {"mode": "answer", "question": ..., "question_date": ..., "context": ...}
  {"mode": "judge", "prompt": "<filled judge prompt>"}

and expects {"output": "<text>"} on stdout. The core stays LLM-free; this
bridge is the only place a model is invoked.

Requires: pip install anthropic. Credentials resolve per the SDK chain
(ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile).

Environment knobs:
  LME_MODEL       model id (default claude-opus-4-8)
  LME_MAX_TOKENS  answer cap (default 512; judge is fixed small)
"""
from __future__ import annotations

import json
import os
import sys

import anthropic

MODEL = os.environ.get("LME_MODEL", "claude-opus-4-8")
MAX_TOKENS = int(os.environ.get("LME_MAX_TOKENS", "512"))

ANSWER_SYSTEM = (
    "You answer questions about a user's chat history. The [MEMORY PACKET] "
    "below was retrieved from that history; timestamps in [Date: ...] "
    "prefixes and event frames are authoritative. Answer concisely from the "
    "packet. If the packet does not contain the information needed, say the "
    "information is not available rather than guessing."
)


def build_request(payload: dict) -> dict:
    if payload.get("mode") == "judge":
        return {
            "model": MODEL,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": payload["prompt"]}],
        }
    user = (
        f"Current date: {payload.get('question_date', 'unknown')}\n\n"
        f"[MEMORY PACKET]\n{payload.get('context', '')}\n\n"
        f"Question: {payload['question']}"
    )
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": ANSWER_SYSTEM,
        "messages": [{"role": "user", "content": user}],
    }


def main() -> int:
    payload = json.loads(sys.stdin.read())
    client = anthropic.Anthropic(max_retries=5)
    response = client.messages.create(**build_request(payload))
    if response.stop_reason == "refusal":
        text = "[refused]"
    else:
        text = "".join(
            block.text for block in response.content if block.type == "text"
        )
    json.dump({"output": text}, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
