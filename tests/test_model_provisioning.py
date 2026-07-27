"""Choosing a model during setup must be the whole installation.

These checks cover the parts that do not need a real 2.5 GB download: digest
verification, the configuration the choice produces, and the supervisor's
refusal to touch a runtime it did not provision.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from joiny_mnemonic import managed_runtime
from joiny_mnemonic.configuration import validate_configuration
from joiny_mnemonic.extraction import ExtractorConfig
from joiny_mnemonic.extractor_backend import validate_backend
from joiny_mnemonic.installer import run_setup
from joiny_mnemonic.model_provisioning import (
    MODEL_CATALOG,
    ProvisioningError,
    backend_block,
    download,
    ensure_model,
)

PAYLOAD = b"gguf-bytes" * 512
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib naming
        body = PAYLOAD if self.path != "/health" else b"{}"
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class _Server:
    def __enter__(self):
        self.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __exit__(self, *_exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_a_verified_artifact_lands_and_is_reused(self):
        with _Server() as url:
            target = self.root / "model.gguf"
            download(f"{url}/model.gguf", target, DIGEST)
            self.assertEqual(target.read_bytes(), PAYLOAD)
            # Second call must not need the network at all.
            download("http://127.0.0.1:1/gone", target, DIGEST)

    def test_a_mismatching_artifact_never_lands(self):
        with _Server() as url:
            target = self.root / "model.gguf"
            with self.assertRaises(ProvisioningError) as caught:
                download(f"{url}/model.gguf", target, "0" * 64)
            self.assertEqual(caught.exception.code, "artifact_digest_mismatch")
            self.assertFalse(target.exists())
            self.assertEqual(list(self.root.glob("*.part")), [])

    def test_an_existing_file_with_the_wrong_digest_is_refused(self):
        target = self.root / "model.gguf"
        target.write_bytes(b"tampered")
        with self.assertRaises(ProvisioningError) as caught:
            download("http://127.0.0.1:1/gone", target, DIGEST)
        self.assertEqual(caught.exception.code, "artifact_digest_mismatch")

    def test_unknown_model_is_refused_by_name(self):
        with self.assertRaises(ProvisioningError) as caught:
            ensure_model("llama-99b", home=self.root)
        self.assertEqual(caught.exception.code, "unknown_model")


class ProvisionedIdentityTest(unittest.TestCase):
    def test_the_backend_block_pins_the_weights_and_moves_the_identity(self):
        endpoint = "http://127.0.0.1:8127"
        blocks = {
            key: backend_block(spec, endpoint) for key, spec in MODEL_CATALOG.items()
        }
        hashes = set()
        for key, block in blocks.items():
            validated = validate_backend(block)
            self.assertTrue(validated.revision.startswith("sha256:"))
            self.assertIn(
                validated.revision.removeprefix("sha256:"),
                MODEL_CATALOG[key].sha256,
            )
            hashes.add(ExtractorConfig.for_backend(validated).canonical_hash)
        self.assertEqual(len(hashes), len(MODEL_CATALOG))


class SetupProvisioningTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.calls: list[str] = []

    def _provisioner(self, key, *, home, progress):
        self.calls.append(key)
        progress(f"fetched {key}")
        spec = MODEL_CATALOG[key]
        return {"backend": backend_block(spec, "http://127.0.0.1:8127")}

    def test_choosing_a_model_installs_the_connector_and_configures_it(self):
        project = self.root / "project"
        project.mkdir()
        result = run_setup(
            project,
            agents=("claude-code",),
            plugins=(),
            scope="project",
            install_hook_adapters=False,
            install_plugins=False,
            extractor_model="qwen3-4b",
            provisioner=self._provisioner,
            home=self.root / "home",
        )
        self.assertEqual(self.calls, ["qwen3-4b"])
        # The transport plugin comes with the model; the user picks one thing.
        self.assertIn("local-llm", " ".join(map(str, result.plugins)))
        config = json.loads(
            (project / ".joiny-mnemonic" / "config.json").read_text(encoding="utf-8")
        )
        extractor = validate_configuration(config)["extractor"]
        self.assertEqual(extractor["name"], "local-llm")
        self.assertEqual(extractor["backend"]["model"], "qwen3-4b")
        self.assertFalse(extractor["requested_enabled"])

    def test_an_unknown_model_choice_is_refused_before_anything_is_installed(self):
        with self.assertRaises(ValueError):
            run_setup(
                self.root / "project2",
                agents=("claude-code",),
                extractor_model="llama-99b",
                provisioner=self._provisioner,
                home=self.root / "home",
            )
        self.assertEqual(self.calls, [])


class SupervisorTest(unittest.TestCase):
    def test_a_runtime_we_did_not_provision_is_left_alone(self):
        foreign = {"endpoint": "http://127.0.0.1:9999/v1"}
        state = {"endpoint": "http://127.0.0.1:8127", "model": "qwen3-4b"}
        self.assertIsNone(managed_runtime.ensure_running(foreign, state=state))
        self.assertIsNone(managed_runtime.ensure_running(None, state=state))

    def test_a_live_runtime_is_reused_instead_of_started_again(self):
        with _Server() as url:
            port = int(url.rsplit(":", 1)[1])
            state = {
                "endpoint": url,
                "model": "qwen3-4b",
                "port": port,
                "server_binary": "should-not-be-launched",
                "model_path": "should-not-be-read",
            }
            endpoint = managed_runtime.ensure_running(
                {"endpoint": f"{url}/v1"}, state=state
            )
            self.assertEqual(endpoint, url)

    def test_missing_weights_fail_loudly_rather_than_starting_a_blank_server(self):
        state = {
            "endpoint": "http://127.0.0.1:1",
            "model": "qwen3-4b",
            "port": 1,
            "server_binary": "llama-server.exe",
            "model_path": str(Path(tempfile.gettempdir()) / "absent-weights.gguf"),
        }
        with self.assertRaises(ProvisioningError) as caught:
            managed_runtime.ensure_running(
                {"endpoint": "http://127.0.0.1:1/v1"}, state=state
            )
        self.assertEqual(caught.exception.code, "weights_missing")


class MissingExtractorTest(unittest.TestCase):
    """Provisioning puts a connector on nearly every machine, so whether a
    misconfigured extractor is fatal must not depend on what else is installed."""

    def test_a_configured_but_absent_extractor_leaves_the_strict_path_working(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            from joiny_mnemonic.service import MemoryService

            with MemoryService(
                root / "memory.db",
                project_root=root,
                extractor_name="absent-extractor",
            ) as service:
                status = service.extraction.status()
                self.assertFalse(status.extractor_available)
                self.assertIn(
                    "absent-extractor", service.extraction.last_wakeup_error or ""
                )
                # The path that does not need an extractor still works.
                self.assertIsNotNone(service.store.active_policy)


if __name__ == "__main__":
    unittest.main()
