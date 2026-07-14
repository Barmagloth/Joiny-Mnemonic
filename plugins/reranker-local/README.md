# joiny-mnemonic-reranker-local

Optional final-stage reranking for Joiny-Mnemonic retrieval using a local
cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2`, ~80 MB, CPU-friendly).

Benchmark calibration on LongMemEval-S probes: doubled multi-session accuracy
(6/20 → 10/20) and lifted packed gold-session coverage from ~76% to 95% by
pulling relevant-but-lexically-weak fragments above the packing budget line.

Install:

```bash
pip install ./plugins/reranker-local
```

When installed, `RetrievalEngine.search` reranks the full fused candidate
ordering before the limit is applied; each hit carries a
`metadata["rerank"]` block (plugin, score, pre/post rank) for auditability.
Absence of the plugin — or any failure inside it — leaves the deterministic
fused ordering untouched.

Model override: `JOINY_MNEMONIC_RERANKER_MODEL`.
