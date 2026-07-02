# Context limits and handoff policy

Joiny-Mnemonic separates the agent host from the model profile. `codex`, `claude-code`, `opencode`
and `openhands` select the hook adapter; the profile selects the model's context capacity and the
operational thresholds. Multiple agents in the same project can therefore use different limits.

## Configuration

Project installation writes `.joiny-mnemonic/context-limits.json`. Global installation writes
`~/.joiny-mnemonic/context-limits.json`, or the path in `JOINY_MNEMONIC_LIMITS_FILE`. Project
configuration has precedence over global configuration. Legacy branch policies in SQLite remain a
fallback only when the current agent has no file-based entry.

```powershell
joiny-mnemonic install-hooks codex --profile gpt-5.2-codex
joiny-mnemonic install-hooks opencode --profile deepseek-v3.1 `
  --handoff-tokens 48000 --reserve-tokens 16000
joiny-mnemonic context-profiles
```

All installation limits are optional overrides: `--context-window`, `--snapshot-ratio`,
`--compact-ratio`, `--handoff-ratio`, `--hard-limit-ratio`, `--handoff-tokens`,
`--reserve-tokens`, and `--min-action-events`. Use `--profile custom` when no bundled model matches.
Re-running an installer without these arguments preserves manually edited values.

The generated file is intentionally plain JSON:

```json
{
  "schema_version": 1,
  "agents": {
    "codex": {
      "profile": "gpt-5.2-codex",
      "limits": {
        "context_window_tokens": 400000,
        "snapshot_ratio": 0.3,
        "compact_ratio": 0.5,
        "handoff_ratio": 0.7,
        "hard_limit_ratio": 0.9,
        "recommended_handoff_tokens": 128000,
        "reserve_tokens": 64000,
        "min_action_interval_events": 20
      }
    }
  }
}
```

## Threshold calculation

The advertised context window and the recommended handoff are deliberately separate. For a
profile, the governor computes:

```text
physical_handoff = min(context_window * handoff_ratio, context_window - reserve_tokens)
handoff          = min(physical_handoff, recommended_handoff_tokens)
hard_limit       = min(context_window * hard_limit_ratio, context_window - reserve_tokens)
```

If the absolute handoff cap lowers `physical_handoff`, snapshot and compaction thresholds are
scaled down by the same factor while preserving their order. This keeps checkpointing ahead of
the recommendation instead of waiting for a fixed percentage of an oversized advertised window.

At snapshot, the hook emits `[CONTEXT CHECKPOINT]` and does not recommend a new session. At
handoff it emits `[CONTEXT HANDOFF RECOMMENDED]`; at the hard limit it emits
`[CONTEXT HANDOFF REQUIRED]`. The messages are event-driven and agent-neutral. Resume packets no
longer contain an unconditional self-promotional instruction.

## Bundled profiles

| Profile | Advertised context | Default handoff cap | Vendor source |
|---|---:|---:|---|
| `claude-sonnet-4.6` | 1,000,000 | 200,000 | [Anthropic context windows](https://platform.claude.com/docs/en/docs/build-with-claude/context-windows) |
| `gpt-5.2-codex` | 400,000 | 128,000 | [OpenAI model page](https://platform.openai.com/docs/models/gpt-5.2-codex) |
| `gemini-2.5-pro` | 1,048,576 | 128,000 | [Google model page](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-pro) |
| `qwen3-coder` | 262,144 | 64,000 | [Qwen model card](https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct) |
| `deepseek-v3.1` | 131,072 | 48,000 | [DeepSeek model card](https://huggingface.co/deepseek-ai/DeepSeek-V3.1) |
| `llama-4-scout` | 10,000,000 | 128,000 | [Meta announcement](https://ai.meta.com/blog/llama-4-multimodal-intelligence/) |
| `mistral-large-3` | 262,144 | 64,000 | [Mistral limits](https://docs.mistral.ai/resources/known-limitations) |

The advertised windows above are vendor limits. The handoff caps are conservative product
defaults, not claims that each model starts degrading at exactly that token. There is no honest
single "real degradation limit" for a model: it changes with task, evidence position, irrelevant
context, tokenizer, reasoning-token accounting, host compaction, and model revision.

## What research supports

The evidence supports using an empirical safety cap, but not deriving it from parameter count:

- [RULER](https://arxiv.org/abs/2404.06654) found that only about half of evaluated models claiming
  at least 32K context maintained satisfactory performance at 32K on its broader task set.
- [NoLiMa](https://arxiv.org/abs/2502.05167) removed literal retrieval cues; at 32K, most evaluated
  models fell below half of their short-context baseline, while GPT-4o fell from 99.3% to 69.7%.
- [Lost in the Middle](https://aclanthology.org/2024.tacl-1.9/) showed strong positional effects:
  information in the middle is often used less reliably than information at the beginning or end.
- [Why Does the Effective Context Length Fall Short?](https://arxiv.org/abs/2410.18745) attributes
  much of the open-model gap to positional distributions during training and reports effective
  lengths often below half of training length.
- [Context Length Alone Hurts](https://arxiv.org/abs/2510.05381) reports degradation even with
  perfect retrieval, showing that retrieval quality alone is not a sufficient predictor.

Larger parameter counts can correlate with better capability inside controlled model families,
but they do not determine context capacity or the degradation point. Architecture, positional
encoding, long-context training distribution and post-training matter directly; Mixture-of-Experts
models also make a single parameter count ambiguous. Consequently, the bundled limits use
published context capacities plus conservative operational caps. Production deployments should
calibrate `recommended_handoff_tokens` against their own resume-quality and task-completion tests.
