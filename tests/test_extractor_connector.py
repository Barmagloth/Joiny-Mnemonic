"""Stage 6: the model-agnostic extractor connector.

The contract under test is "swap the model by configuration alone": the same
installed plugin, driven by two configuration blocks, must talk to two models,
and each swap must move ``ExtractorConfig.canonical_hash`` so a signed report
can never be carried over to a system that was not measured (JM-INV-007).
"""

from __future__ import annotations

import importlib.util
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from joiny_mnemonic.configuration import validate_configuration
from joiny_mnemonic.extraction import (
    ExtractorConfig,
    parse_candidates,
    resolve_extractor_config,
    validate_candidate,
)
from joiny_mnemonic.extractor_backend import (
    CANDIDATE_JSON_SCHEMA,
    VERDICT_JSON_SCHEMA,
    BackendConfigurationError,
    validate_backend,
)
from joiny_mnemonic.models import Event


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT / "plugins" / "local-llm" / "src" / "joiny_mnemonic_local_llm" / "__init__.py"
)
SPEC = importlib.util.spec_from_file_location("joiny_mnemonic_local_llm_test", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


EVENT_TEXT = "Мы решили использовать SQLite как единственную основную базу."
CANDIDATES = {
    "candidates": [
        {
            "memory_type": "decision",
            "normalized_content": "SQLite остаётся единственной основной базой",
            "evidence_quote": "решили использовать SQLite как единственную основную базу",
            "confidence": 0.93,
        }
    ]
}


def _event(content: str = EVENT_TEXT) -> Event:
    return Event(
        seq=1,
        id="evt_connector",
        branch_id="main",
        session_id="connector",
        kind="message",
        role="user",
        origin_channel="public_api",
        origin_adapter=None,
        content=content,
        payload={},
        files=(),
        created_at="2026-07-27T00:00:00+00:00",
        previous_hash=None,
        content_hash="connector",
        chain_hash="connector-1",
    )


class _Runtime(BaseHTTPRequestHandler):
    """Stands in for llama.cpp / vLLM on loopback; records what it was asked."""

    requests: list[tuple[str, dict]] = []
    completion_text = json.dumps(CANDIDATES, ensure_ascii=False)
    envelope_override: dict | None = None

    def log_message(self, *args):  # silence the test run
        return

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).requests.append((self.path, body))
        if type(self).envelope_override is not None:
            envelope = type(self).envelope_override
        elif self.path.endswith("/chat/completions"):
            envelope = {
                "choices": [{"message": {"content": type(self).completion_text}}]
            }
        else:
            envelope = {"content": type(self).completion_text}
        payload = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class ConnectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Runtime)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        _Runtime.requests = []
        _Runtime.completion_text = json.dumps(CANDIDATES, ensure_ascii=False)
        _Runtime.envelope_override = None

    def backend(self, **overrides):
        value = {
            "transport": "openai_compatible",
            "endpoint": f"{self.base}/v1",
            "model": "qwen3-4b",
            "revision": "Q4_K_M-2026-07",
            "inference": {"temperature": 0.0},
        }
        value.update(overrides)
        return value

    # --- identity -------------------------------------------------------

    def test_model_swap_changes_canonical_hash(self):
        qwen = ExtractorConfig.for_backend(validate_backend(self.backend()))
        gemma = ExtractorConfig.for_backend(
            validate_backend(self.backend(model="gemma-3-4b"))
        )
        self.assertNotEqual(qwen.canonical_hash, gemma.canonical_hash)
        self.assertEqual(qwen.model_identity, "qwen3-4b")
        self.assertEqual(gemma.model_identity, "gemma-3-4b")

    def test_revision_transport_and_inference_swaps_change_canonical_hash(self):
        base = ExtractorConfig.for_backend(validate_backend(self.backend()))
        for override in (
            {"revision": "Q8_0-2026-07"},
            {"transport": "llama_cpp"},
            {"inference": {"temperature": 0.4}},
            {"endpoint": f"http://localhost:{self.server.server_port}/v1"},
        ):
            with self.subTest(override=override):
                other = ExtractorConfig.for_backend(
                    validate_backend(self.backend(**override))
                )
                self.assertNotEqual(base.canonical_hash, other.canonical_hash)

    def test_descriptor_pins_prompt_and_schema_hashes(self):
        descriptor = ExtractorConfig.for_backend(
            validate_backend(self.backend())
        ).descriptor()
        self.assertIn("prompt_hash", descriptor)
        self.assertIn("schema_hash", descriptor)
        self.assertEqual(descriptor["backend"]["model"], "qwen3-4b")

    # --- refusals -------------------------------------------------------

    def test_remote_endpoint_is_refused_until_a_remote_transport_exists(self):
        with self.assertRaises(BackendConfigurationError) as caught:
            validate_backend(self.backend(endpoint="https://api.example.com/v1"))
        self.assertEqual(caught.exception.code, "remote_backend_not_implemented")

    def test_unknown_transport_is_refused(self):
        with self.assertRaises(BackendConfigurationError) as caught:
            validate_backend(self.backend(transport="carrier_pigeon"))
        self.assertEqual(caught.exception.code, "unsupported_transport")

    def test_missing_model_is_refused(self):
        with self.assertRaises(BackendConfigurationError) as caught:
            validate_backend(self.backend(model="  "))
        self.assertEqual(caught.exception.code, "missing_model")

    def test_installer_configuration_validates_and_normalizes_the_backend(self):
        validated = validate_configuration(
            {
                "version": 2,
                "scope": "project",
                "agents": ["claude-code"],
                "plugins": ["local-llm"],
                "extractor": {
                    "requested_enabled": False,
                    "name": "local-llm",
                    "backend": self.backend(),
                },
            }
        )
        self.assertEqual(validated["extractor"]["backend"]["model"], "qwen3-4b")
        with self.assertRaises(ValueError):
            validate_configuration(
                {
                    "version": 2,
                    "scope": "project",
                    "agents": [],
                    "plugins": [],
                    "extractor": {
                        "requested_enabled": False,
                        "name": "local-llm",
                        "backend": self.backend(endpoint="https://api.example.com/v1"),
                    },
                }
            )

    # --- transport ------------------------------------------------------

    def _extract(self, backend_value):
        plugin = MODULE.LocalLLMExtractor()
        config = ExtractorConfig.for_backend(validate_backend(backend_value))
        raw = plugin.extract(_event(), context=(), config=config.descriptor())
        return raw, config

    def test_openai_compatible_transport_produces_a_valid_candidate(self):
        raw, config = self._extract(self.backend())
        candidate = parse_candidates(raw)[0]
        valid = validate_candidate(candidate, _event(), threshold=config.auto_threshold)
        self.assertEqual(valid.memory_type, "decision")
        self.assertEqual(valid.initial_status, "auto")
        path, body = _Runtime.requests[-1]
        self.assertTrue(path.endswith("/chat/completions"))
        self.assertEqual(body["model"], "qwen3-4b")
        self.assertEqual(
            body["response_format"]["json_schema"]["schema"], CANDIDATE_JSON_SCHEMA
        )
        self.assertIn(EVENT_TEXT, body["messages"][0]["content"])

    def test_llama_cpp_transport_constrains_decoding_with_the_core_schema(self):
        raw, _ = self._extract(self.backend(transport="llama_cpp", endpoint=self.base))
        self.assertEqual(parse_candidates(raw)[0].memory_type, "decision")
        path, body = _Runtime.requests[-1]
        self.assertTrue(path.endswith("/completion"))
        self.assertEqual(body["json_schema"], CANDIDATE_JSON_SCHEMA)
        self.assertIn(EVENT_TEXT, body["prompt"])

    def test_one_installed_plugin_serves_two_models_without_code_changes(self):
        plugin = MODULE.LocalLLMExtractor()
        seen = []
        for model in ("qwen3-4b", "gemma-3-4b"):
            config = ExtractorConfig.for_backend(
                validate_backend(self.backend(model=model))
            )
            plugin.extract(_event(), context=(), config=config.descriptor())
            seen.append((_Runtime.requests[-1][1]["model"], config.canonical_hash))
        self.assertEqual([item[0] for item in seen], ["qwen3-4b", "gemma-3-4b"])
        self.assertNotEqual(seen[0][1], seen[1][1])

    def test_context_events_are_labelled_and_never_become_evidence(self):
        plugin = MODULE.LocalLLMExtractor()
        config = ExtractorConfig.for_backend(validate_backend(self.backend()))
        plugin.extract(
            _event(),
            context=(_event("Ранее обсуждали Postgres."),),
            config=config.descriptor(),
        )
        prompt = _Runtime.requests[-1][1]["messages"][0]["content"]
        self.assertIn("CONTEXT 1:", prompt)
        self.assertIn("CURRENT EVENT:", prompt)
        self.assertLess(prompt.index("CONTEXT 1:"), prompt.index("CURRENT EVENT:"))

    def test_malformed_envelope_fails_closed_with_a_code(self):
        _Runtime.envelope_override = {"unexpected": True}
        with self.assertRaises(MODULE.ExtractorTransportError) as caught:
            self._extract(self.backend())
        self.assertEqual(caught.exception.code, "backend_malformed_envelope")

    def test_unconfigured_backend_refuses_before_any_request(self):
        plugin = MODULE.LocalLLMExtractor()
        config = ExtractorConfig(
            model_identity="x", model_version="y", inference_parameters={}
        )
        with self.assertRaises(MODULE.ExtractorTransportError) as caught:
            plugin.extract(_event(), context=(), config=config.descriptor())
        self.assertEqual(caught.exception.code, "backend_not_configured")
        self.assertEqual(_Runtime.requests, [])

    # --- verifier second pass -------------------------------------------

    def _verify(self, holds: bool = True, *, backend_value=None):
        _Runtime.completion_text = json.dumps(
            {"holds": holds, "reason": "holds" if holds else "hypothetical"}
        )
        plugin = MODULE.LocalLLMExtractor()
        config = ExtractorConfig.for_backend(
            validate_backend(backend_value or self.backend()), verify_candidates=True
        )
        return plugin.verify(
            _event(),
            memory_type="decision",
            normalized_content="SQLite остаётся единственной основной базой",
            evidence_quote="решили использовать SQLite",
            config=config.descriptor(),
        )

    def test_verification_asks_the_core_verdict_schema_over_the_same_transport(self):
        self.assertIs(self._verify(True), True)
        self.assertIs(self._verify(False), False)
        path, body = _Runtime.requests[-1]
        self.assertTrue(path.endswith("/chat/completions"))
        self.assertEqual(
            body["response_format"]["json_schema"]["schema"], VERDICT_JSON_SCHEMA
        )
        prompt = body["messages"][0]["content"]
        self.assertIn("CANDIDATE:", prompt)
        self.assertIn(EVENT_TEXT, prompt)
        # The candidate is data below the instruction block, not part of it.
        self.assertLess(prompt.index("CURRENT EVENT:"), prompt.index("CANDIDATE:"))

    def test_verification_over_the_native_transport_uses_the_same_schema(self):
        self._verify(True, backend_value=self.backend(
            transport="llama_cpp", endpoint=self.base
        ))
        path, body = _Runtime.requests[-1]
        self.assertTrue(path.endswith("/completion"))
        self.assertEqual(body["json_schema"], VERDICT_JSON_SCHEMA)

    def test_a_malformed_verdict_fails_closed_rather_than_defaulting(self):
        # Defaulting to true would trust an answer the model never gave;
        # defaulting to false would pass a broken runtime off as a quality result.
        for text in ("not json at all", json.dumps({"reason": "holds"})):
            with self.subTest(text=text):
                _Runtime.completion_text = text
                plugin = MODULE.LocalLLMExtractor()
                config = ExtractorConfig.for_backend(
                    validate_backend(self.backend()), verify_candidates=True
                )
                with self.assertRaises(MODULE.ExtractorTransportError) as caught:
                    plugin.verify(
                        _event(),
                        memory_type="decision",
                        normalized_content="x",
                        evidence_quote="решили",
                        config=config.descriptor(),
                    )
                self.assertEqual(caught.exception.code, "backend_malformed_verdict")

    def test_configuration_carries_the_verifier_flag_into_the_identity(self):
        validated = validate_configuration(
            {
                "version": 2,
                "scope": "project",
                "agents": ["claude-code"],
                "plugins": ["local-llm"],
                "extractor": {
                    "requested_enabled": False,
                    "name": "local-llm",
                    "backend": self.backend(),
                    "verify_candidates": True,
                },
            }
        )
        self.assertIs(validated["extractor"]["verify_candidates"], True)
        plugin = MODULE.LocalLLMExtractor()
        resolved = resolve_extractor_config(validated["extractor"], plugin)
        self.assertIs(resolved.verify_candidates, True)
        without = resolve_extractor_config({"backend": self.backend()}, plugin)
        self.assertNotEqual(resolved.canonical_hash, without.canonical_hash)

    def test_service_resolution_prefers_the_configured_backend(self):
        plugin = MODULE.LocalLLMExtractor()
        from_config = resolve_extractor_config({"backend": self.backend()}, plugin)
        legacy = resolve_extractor_config({}, plugin)
        self.assertEqual(from_config.model_identity, "qwen3-4b")
        self.assertEqual(legacy.model_identity, "unconfigured")
        self.assertIsNone(resolve_extractor_config({"backend": self.backend()}, None))


if __name__ == "__main__":
    unittest.main()
