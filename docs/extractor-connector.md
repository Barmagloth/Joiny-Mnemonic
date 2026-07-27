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

## Installed, not assembled

Choosing a model is part of `joiny-mnemonic setup`, not a separate chore. The
setup question lists the catalogue; picking an entry downloads the weights and a
pinned `llama.cpp` runtime into `~/.joiny-mnemonic`, installs the `local-llm`
plugin, and writes the resulting backend block into the project configuration.
Nothing else has to be started by hand: the server is launched lazily the first
time there is a backlog to extract, and reused while it stays healthy.

```bash
joiny-mnemonic setup --yes --extractor-model qwen3-4b
```

Two properties make this safe rather than merely convenient:

- **Every downloaded artifact is content-pinned.** The catalogue records the
  sha256 of each weight file and each runtime archive, and a byte that does not
  match is refused (`artifact_digest_mismatch`) instead of landing on disk.
  Publisher metadata is not trusted for this — a HuggingFace ETag is a Xet
  hash, not the file digest.
- **The weights are part of the identity.** The generated backend block carries
  `revision: "sha256:<first 16 hex of the weight digest>"`, so re-provisioning
  different bytes moves `canonical_hash` and invalidates signed evaluation
  reports, exactly as a manual model swap does.

Provisioning is currently implemented for Windows on x86-64; other platforms are
refused with `platform_not_provisioned` rather than guessing at a build. The
supervisor only ever starts the endpoint recorded in
`~/.joiny-mnemonic/managed-runtime.json`; a backend you serve yourself is left
alone, and a runtime that fails to start is reported as a wakeup error rather
than failing the host interaction (JM-INV-008).

## The recipe (serving a model yourself)

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

## Measuring a candidate

The target is frozen before the run, not after it, and the run is measured
against that file:

```bash
PYTHONPATH=src python benchmarks/stage6_extractor_eval.py --backend backend.json --freeze target.json
```

```bash
PYTHONPATH=src python benchmarks/stage6_extractor_eval.py --backend backend.json --target target.json
```

`backend.json` holds the same block as the `backend` key above. The frozen file
records the corpus bytes, the extractor name and version, the hashed config, the
memory types the thresholds apply to, the thresholds themselves and the version
of the checking code. If any of it differs at run time the run is refused with
the exact mismatch listed — a report about a different system is not a weaker
result, it is a result about something else.

Add `--limit N` for the cheap smoke slice (reachability, JSON validity,
empty-output rate, latency) before paying for a full run. The report lands in
`benchmarks/results/stage6/` under a dated name and is never rewritten;
`latest.json` only points at it.

### What the first measurements showed (2026-07-27)

On the full development corpora (70 EN + 70 RU, one run per configuration, not
held-out), an untyped prompt made `qwen3-4b` label preferences as `decision` and
score 0.06 / 0.08 recall, while `gemma-3-4b` reached 0.85 / 0.75 on EN. Adding
`memory_type` definitions to the shared prompt moved qwen to 0.852 precision /
0.963 recall (EN) and 0.882 / 0.849 (RU) with zero false-trusted candidates, and
left gemma at 0.831 / 0.907 (EN) and 0.756 / 0.630 (RU). Neither passes the
gate — both fall short of the 0.90 precision threshold.

Two things this illustrates about the contract. The prompt lives in the core, so
that improvement applied to both models by construction and the comparison
stayed meaningful. And because the prompt is hashed into the identity, the
change invalidated the frozen targets: new targets had to be frozen and the old
reports remain valid statements about the older system rather than being
silently reinterpreted.

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

```bash
PYTHONPATH=src python -m unittest tests.test_model_provisioning -v
```

Covers provisioning as installation: digest verification (including refusal of
an already-present file that does not match the pin), reuse of a verified
artifact without touching the network, the weight digest moving the backend
identity, `setup --extractor-model` producing a valid configured backend, and
the supervisor leaving a hand-served backend alone.

```bash
PYTHONPATH=src python -m unittest tests.test_stage6_extractor_gate -v
```

Covers the evaluation gate: refusal of a run whose model, revision, corpus,
scored types, thresholds or checker version differ from the frozen target; the
per-language thresholds; the refusal to rewrite a published dated report; and
`latest.json` being a pointer. The end-to-end runner check drives a stub
extractor, so it proves the wiring only — it makes no claim about any model.
