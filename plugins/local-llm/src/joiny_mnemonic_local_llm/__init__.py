"""Model-agnostic local extractor backend.

This plugin owns transport only. What is asked (the prompt) and what shape is
accepted (the candidate JSON schema) belong to the core, so swapping the model
is a configuration edit and never a code change.

Supported local runtimes:

- ``openai_compatible`` — any server exposing ``POST /chat/completions`` with
  ``response_format: json_schema`` (llama.cpp server, vLLM, LM Studio, Ollama's
  OpenAI-compatible endpoint).
- ``llama_cpp`` — llama.cpp's native ``POST /completion`` with ``json_schema``,
  which the server converts into a decoding grammar.

Both constrain decoding to the core schema; a model that cannot honour the
schema fails loudly instead of returning prose.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping

from joiny_mnemonic.extractor_backend import (
    CANDIDATE_JSON_SCHEMA,
    BackendConfig,
    render_prompt,
    validate_backend,
)


class ExtractorTransportError(ValueError):
    """Transport or protocol failure; carries a code for the failure ledger."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LocalLLMExtractor:
    name = "local-llm"

    def __init__(self, backend: BackendConfig | None = None) -> None:
        self._backend = backend

    @property
    def model_identity(self) -> str:
        return self._backend.model if self._backend else "unconfigured"

    @property
    def model_version(self) -> str:
        return self._backend.revision if self._backend else "unconfigured"

    @property
    def inference_parameters(self) -> dict[str, Any]:
        return dict(self._backend.inference) if self._backend else {}

    def _resolve(self, config: Mapping[str, Any]) -> BackendConfig:
        declared = config.get("backend")
        if declared:
            return validate_backend(declared)
        if self._backend is not None:
            return self._backend
        raise ExtractorTransportError(
            "backend_not_configured",
            "local-llm requires an extractor backend block in the configuration",
        )

    def _post(self, url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            raise ExtractorTransportError(
                "backend_http_error",
                f"extractor runtime returned HTTP {exc.code}",
            ) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ExtractorTransportError(
                "backend_unreachable", f"extractor runtime is unreachable: {exc}"
            ) from exc
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExtractorTransportError(
                "backend_malformed_envelope",
                "extractor runtime returned a non-JSON envelope",
            ) from exc
        if not isinstance(decoded, dict):
            raise ExtractorTransportError(
                "backend_malformed_envelope",
                "extractor runtime envelope must be a JSON object",
            )
        return decoded

    def _completion(self, backend: BackendConfig, prompt: str) -> str:
        inference = dict(backend.inference)
        if backend.transport == "openai_compatible":
            body = {
                "model": backend.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "memory_candidates",
                        "strict": True,
                        "schema": CANDIDATE_JSON_SCHEMA,
                    },
                },
                **inference,
            }
            envelope = self._post(
                f"{backend.endpoint}/chat/completions",
                body,
                backend.request_timeout_seconds,
            )
            try:
                return str(envelope["choices"][0]["message"]["content"])
            except (KeyError, IndexError, TypeError) as exc:
                raise ExtractorTransportError(
                    "backend_malformed_envelope",
                    "chat completion envelope has no message content",
                ) from exc
        body = {
            "prompt": prompt,
            "json_schema": CANDIDATE_JSON_SCHEMA,
            "stream": False,
            **inference,
        }
        envelope = self._post(
            f"{backend.endpoint}/completion", body, backend.request_timeout_seconds
        )
        if "content" not in envelope:
            raise ExtractorTransportError(
                "backend_malformed_envelope",
                "completion envelope has no content field",
            )
        return str(envelope["content"])

    def extract(self, event, *, context, config: Mapping[str, Any]) -> str:
        backend = self._resolve(config)
        prompt = render_prompt(
            event.content, tuple(item.content for item in context)
        )
        return self._completion(backend, prompt)


def create_plugin(**_: Any) -> LocalLLMExtractor:
    return LocalLLMExtractor()
