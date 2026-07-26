# Extractor connector — swapping the model without writing code

The extractor only ever proposes candidates. It cannot create confirmed memory,
`agent_finalized` outcomes, or `host_logical_user` authority — that boundary is
owned by post-factum finalization (ROADMAP stage 5) and is unaffected by which
model runs here.

## What owns what

| Concern | Owner |
|---|---|
| Prompt text, candidate JSON schema, allowed memory types | core (`joiny_mnemonic/extractor_backend.py`) |
| Backend validation and identity hashing | core (`ExtractorConfig.for_backend`) |
| HTTP transport to a local runtime | plugin `local-llm` |
| Which model runs, and with what parameters | your configuration file |

Because the prompt and schema live in the core, two models are asked exactly the
same question — that is what makes an evaluation comparison meaningful.

## The recipe

1. Serve the model from any local runtime that speaks one of the supported
   protocols. Examples:

   ```bash
   llama-server --model qwen3-4b-instruct-q4_k_m.gguf --port 8080
   ```

2. Install the connector plugin once:

   ```bash
   pip install ./plugins/local-llm
   ```

3. Point the configuration at the model. Project scope is
   `.joiny-mnemonic/config.json`:

   ```json
   {
     "version": 2,
     "scope": "project",
     "agents": ["claude-code"],
     "plugins": ["local-llm"],
     "extractor": {
       "requested_enabled": false,
       "name": "local-llm",
       "backend": {
         "transport": "openai_compatible",
         "endpoint": "http://127.0.0.1:8080/v1",
         "model": "qwen3-4b",
         "revision": "Q4_K_M-2026-07",
         "inference": {"temperature": 0.0, "max_tokens": 768}
       }
     }
   }
   ```

4. To swap the model, edit `model`, `revision` and `inference`. Nothing else
   changes — no new Python package, no environment variables, no code.

`transport` selects the protocol:

- `openai_compatible` — `POST {endpoint}/chat/completions` with
  `response_format: json_schema` (llama.cpp server, vLLM, LM Studio, Ollama's
  OpenAI-compatible endpoint);
- `llama_cpp` — llama.cpp's native `POST {endpoint}/completion` with
  `json_schema`, which the server compiles into a decoding grammar.

Both constrain decoding to the core schema. A model that cannot honour the
schema fails loudly; it never degrades into prose that later gets guessed at.

## Why a swap invalidates signed reports

`ExtractorConfig.canonical_hash` covers the whole backend descriptor —
transport, endpoint, model, revision and inference parameters — plus the prompt
hash and the schema hash. Any swap produces a different hash, so a report
measured on one system cannot be presented as evidence for another
(`JM-INV-007`). This is deliberately strict: changing the port also changes the
identity, because the report names the exact system that was measured.

## Remote models: contracted, not implemented

A remote model — a hosted API endpoint or a host-CLI bridge such as `claude -p`
or `codex exec` — is designed to be another `transport` behind this same
contract, selected the same way by configuration. It is not implemented yet, so
the configuration refuses a non-loopback endpoint with
`remote_backend_not_implemented` rather than quietly shipping event content off
the machine. When a remote transport lands it inherits the identity rules
above unchanged: a remote swap invalidates local reports exactly like a local
swap, and a remote extractor stays candidate-only.

## Executable checks

```bash
PYTHONPATH=src python -m unittest tests.test_extractor_connector -v
```

Covers: config-only model swap over a real loopback runtime, hash movement on
every identity field, both transports sending the core schema, refusal of remote
endpoints and unknown transports, and fail-closed behaviour on malformed
runtime output.
