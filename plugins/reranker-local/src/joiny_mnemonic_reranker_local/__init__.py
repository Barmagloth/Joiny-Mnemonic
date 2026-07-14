"""Local cross-encoder reranking plugin.

Benchmark calibration (2026-07): a small local cross-encoder is the single
largest retrieval-precision lever below oracle — on our LongMemEval probes it
doubled multi-session accuracy (6/20 -> 10/20) and lifted packed gold-session
coverage from ~76% to 95%. Model is ms-marco-MiniLM-L-6-v2 (~80 MB, runs on
CPU), the same cross-encoder Hindsight ships (Apache-2.0 recipe attribution).

Purely optional and local: no network calls at query time, absence of the
plugin leaves the deterministic fused ordering untouched.
"""
from __future__ import annotations

import os
import threading
from dataclasses import replace
from typing import Any

from joiny_mnemonic.models import RetrievalHit

DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
_MAX_CHARS = 2000  # cross-encoder input truncation per document


class LocalCrossEncoderReranker:
    name = "local-cross-encoder"

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or os.environ.get(
            "JOINY_MNEMONIC_RERANKER_MODEL", DEFAULT_MODEL
        )
        self._model: Any = None
        self._lock = threading.Lock()

    def _ensure_model(self) -> Any:
        with self._lock:
            if self._model is None:
                try:
                    from sentence_transformers import CrossEncoder
                except ImportError as exc:  # pragma: no cover
                    raise RuntimeError(
                        "joiny-mnemonic-reranker-local requires "
                        "sentence-transformers"
                    ) from exc
                self._model = CrossEncoder(self.model_name)
        return self._model

    def rerank(self, query: str, hits: list[RetrievalHit]) -> list[RetrievalHit]:
        if not hits:
            return hits
        model = self._ensure_model()
        scores = model.predict(
            [(query, hit.content[:_MAX_CHARS]) for hit in hits]
        )
        order = sorted(
            range(len(hits)), key=lambda index: float(scores[index]), reverse=True
        )
        reranked = []
        for position, index in enumerate(order):
            hit = hits[index]
            reranked.append(
                replace(
                    hit,
                    metadata={
                        **hit.metadata,
                        "rerank": {
                            "plugin": self.name,
                            "score": round(float(scores[index]), 6),
                            "pre_rank": index + 1,
                            "post_rank": position + 1,
                        },
                    },
                )
            )
        return reranked


def create_plugin(context: Any = None) -> LocalCrossEncoderReranker:
    return LocalCrossEncoderReranker()
