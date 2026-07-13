from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from joiny_mnemonic.cli import build_parser
from joiny_mnemonic.configuration import (
    effective_configuration,
    global_config_path,
    project_config_path,
    write_configuration,
)
from joiny_mnemonic.hooks import InstallResult
from joiny_mnemonic.installer import (
    AgentDetection,
    detect_agents,
    mcp_command,
    plugin_install_spec,
    run_setup,
    select_interactively,
)
from joiny_mnemonic.plugins import PluginRegistry
from joiny_mnemonic.service import MemoryService


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
RUNTIME_ROOT.mkdir(exist_ok=True)


class InstallerTest(unittest.TestCase):
    def project(self) -> Path:
        root = RUNTIME_ROOT / f"installer-{uuid.uuid4().hex}"
        root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        return root

    def test_detection_combines_executables_and_existing_configs(self) -> None:
        root = self.project()
        home = root / "home"
        home.mkdir()
        (root / ".opencode").mkdir()

        def which(command, *, path=None):
            return f"/bin/{command}" if command in {"claude", "codex"} else None

        with patch("joiny_mnemonic.installer.shutil.which", side_effect=which):
            detected = detect_agents(root, environ={"PATH": "bin"}, home=home)
        by_id = {item.id: item for item in detected}
        self.assertTrue(by_id["claude-code"].detected)
        self.assertTrue(by_id["codex"].detected)
        self.assertTrue(by_id["opencode"].detected)
        self.assertFalse(by_id["openhands"].detected)

    def test_configuration_migrates_legacy_intent_and_allows_custom_extractor(self) -> None:
        root = self.project()
        home = root / "home"
        home.mkdir()
        global_path = global_config_path(home=home)
        write_configuration(
            global_path,
            {
                "version": 1,
                "scope": "global",
                "agents": ["codex"],
                "plugins": [],
                "extractor": {"enabled": False, "name": None},
            },
        )
        project_path = project_config_path(root)
        write_configuration(
            project_path,
            {
                "version": 1,
                "scope": "project",
                "agents": ["claude-code"],
                "plugins": ["nuextract-local"],
                "extractor": {"enabled": True, "name": "nuextract-local"},
            },
        )
        selected = effective_configuration(root, home=home)
        self.assertEqual(selected["agents"], ["claude-code"])
        self.assertTrue(selected["extractor"]["requested_enabled"])
        self.assertNotIn("enabled", selected["extractor"])
        custom = write_configuration(
            project_path,
            {
                "version": 2,
                "scope": "project",
                "agents": [],
                "plugins": [],
                "extractor": {
                    "requested_enabled": True,
                    "name": "third-party-extractor",
                },
            },
        )
        self.assertEqual(
            json.loads(custom.read_text(encoding="utf-8"))["extractor"]["name"],
            "third-party-extractor",
        )

    def test_workspace_config_cannot_activate_extraction(self) -> None:
        root = self.project()
        write_configuration(
            project_config_path(root),
            {
                "version": 2,
                "scope": "project",
                "agents": [],
                "plugins": ["nuextract-local"],
                "extractor": {
                    "requested_enabled": True,
                    "name": "nuextract-local",
                },
            },
        )
        registry = PluginRegistry(load_installed=False)
        extractor = SimpleNamespace(
            name="nuextract-local",
            model_identity="fake",
            model_version="1",
            inference_parameters={},
            extract=lambda *args, **kwargs: {"candidates": []},
        )
        registry.register_extractor(extractor)
        with patch.dict(os.environ, {"JOINY_MNEMONIC_EXTRACTOR_ENABLED": "1"}):
            service_context = MemoryService(
                ":memory:", project_root=root, plugins=registry
            )
        with service_context as service:
            self.assertFalse(service.extraction.enabled)
            self.assertIsNone(service.store.active_policy())
            self.assertIs(service.extraction.extractor, extractor)

    def test_active_policy_is_the_only_runtime_extraction_switch(self) -> None:
        root = self.project()
        database = root / "memory.db"
        with MemoryService(database, project_root=root) as bootstrap:
            bootstrap.initialize_project(automatic_extraction_enabled=True)
        registry = PluginRegistry(load_installed=False)
        extractor = SimpleNamespace(
            name="custom-extractor",
            model_identity="fake",
            model_version="1",
            inference_parameters={},
            extract=lambda *args, **kwargs: {"candidates": []},
        )
        registry.register_extractor(extractor)
        write_configuration(
            project_config_path(root),
            {
                "version": 2,
                "scope": "project",
                "agents": [],
                "plugins": [],
                "extractor": {
                    "requested_enabled": False,
                    "name": "custom-extractor",
                },
            },
        )
        with MemoryService(database, project_root=root, plugins=registry) as service:
            self.assertTrue(service.extraction.enabled)
            self.assertEqual(service.extraction.extractor.name, "custom-extractor")
            with self.assertRaisesRegex(ValueError, "cannot override active policy"):
                MemoryService(
                    database,
                    project_root=root,
                    plugins=registry,
                    extractor_enabled=False,
                )
    def test_local_plugin_specs_and_vendor_mcp_commands(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        self.assertEqual(
            Path(plugin_install_spec("knowledge-graph", repository)),
            repository / "plugins" / "knowledge-graph",
        )
        codex = mcp_command(
            "codex", repository, scope="project", python_executable="python-stable"
        )
        self.assertEqual(codex[:5], ["codex", "mcp", "add", "joiny-mnemonic", "--"])
        self.assertIn("python-stable", codex)
        claude = mcp_command(
            "claude-code", repository, scope="global", python_executable="python-stable"
        )
        self.assertIn("user", claude)
        self.assertIsNone(mcp_command("opencode", repository, scope="project"))

    def test_dry_run_plans_without_writing(self) -> None:
        root = self.project()
        result = run_setup(
            root,
            agents=("codex", "opencode"),
            plugins=("knowledge-graph", "nuextract-local"),
            install_mcp=True,
            source_root=Path(__file__).resolve().parents[1],
            dry_run=True,
            python_executable="python-stable",
            environ={"PATH": "bin"},
            home=root / "home",
        )
        self.assertTrue(result.dry_run)
        self.assertEqual(len(result.plugin_installs), 2)
        self.assertEqual(len(result.hooks), 2)
        self.assertFalse(Path(result.configuration_file).exists())
        self.assertFalse((root / "opencode.json").exists())

    def test_setup_installs_components_hooks_mcp_and_initializes(self) -> None:
        root = self.project()
        home = root / "home"
        home.mkdir()
        calls = []

        def runner(command, **kwargs):
            calls.append((list(command), kwargs))
            return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

        hook = InstallResult(
            agent="claude-code",
            files=(str(root / ".claude" / "settings.json"),),
            command="hook",
            status="installed",
        )
        with (
            patch("joiny_mnemonic.installer.install_hooks", return_value=hook),
            patch("joiny_mnemonic.installer.shutil.which", return_value="claude"),
        ):
            result = run_setup(
                root,
                agents=("claude-code",),
                plugins=("knowledge-graph",),
                install_mcp=True,
                source_root=Path(__file__).resolve().parents[1],
                runner=runner,
                environ={"PATH": "bin"},
                home=home,
            )

        self.assertEqual(result.plugin_installs[0]["status"], "installed")
        self.assertEqual(result.mcp[0]["status"], "configured")
        self.assertTrue((root / ".joiny-mnemonic" / "memory.db").exists())
        config = json.loads(Path(result.configuration_file).read_text(encoding="utf-8"))
        self.assertEqual(config["agents"], ["claude-code"])
        self.assertEqual(config["plugins"], ["knowledge-graph"])
        self.assertEqual(calls[0][0][1:4], ["-m", "pip", "install"])
        self.assertEqual(calls[1][0][:4], ["claude", "mcp", "add", "--transport"])

        run_setup(
            root,
            agents=("claude-code",),
            plugins=("knowledge-graph",),
            install_hook_adapters=False,
            install_mcp=False,
            install_plugins=False,
            home=home,
        )
        with MemoryService(result.database, project_root=root) as service:
            self.assertEqual(service.store.list_security_findings(), ())

    def test_fresh_setup_bootstraps_explicit_extraction_policy(self) -> None:
        root = self.project()
        result = run_setup(
            root,
            agents=(),
            plugins=("nuextract-local",),
            install_hook_adapters=False,
            install_plugins=False,
            enable_extraction=True,
        )
        config = json.loads(Path(result.configuration_file).read_text(encoding="utf-8"))
        self.assertEqual(result.plugin_installs[0]["status"], "externally-managed")
        self.assertTrue(config["extractor"]["requested_enabled"])
        self.assertNotIn("enabled", config["extractor"])
        with MemoryService(result.database, project_root=root) as service:
            self.assertTrue(
                service.store.active_policy()["policy"]["automatic_extraction_enabled"]
            )
        self.assertTrue(any("initial TOFU policy" in note for note in result.notes))

    def test_existing_setup_only_appends_idempotent_policy_request(self) -> None:
        root = self.project()
        initial = run_setup(
            root,
            agents=(),
            plugins=("nuextract-local",),
            install_hook_adapters=False,
            install_plugins=False,
        )
        requested = run_setup(
            root,
            agents=(),
            plugins=("nuextract-local",),
            install_hook_adapters=False,
            install_plugins=False,
            enable_extraction=True,
        )
        repeated = run_setup(
            root,
            agents=(),
            plugins=("nuextract-local",),
            install_hook_adapters=False,
            install_plugins=False,
            enable_extraction=True,
        )
        with MemoryService(initial.database, project_root=root) as service:
            active = service.store.active_policy()
            self.assertFalse(active["policy"]["automatic_extraction_enabled"])
            requests = [
                event
                for event in service.store.query_events(kinds=("state",))
                if event.payload.get("operation") == "policy_change_requested"
            ]
            self.assertEqual(len(requests), 1)
            self.assertFalse(service.extraction.enabled)
        self.assertTrue(any("trusted policy approval" in note for note in requested.notes))
        self.assertEqual(requested.notes, repeated.notes)

    def test_extraction_activation_requires_project_nuextract_selection(self) -> None:
        root = self.project()
        with self.assertRaisesRegex(ValueError, "requires the nuextract-local"):
            run_setup(root, agents=(), enable_extraction=True, dry_run=True)
        with self.assertRaisesRegex(ValueError, "requires project scope"):
            run_setup(
                root,
                agents=(),
                plugins=("nuextract-local",),
                scope="global",
                enable_extraction=True,
                dry_run=True,
            )
    def test_opencode_mcp_preserves_existing_configuration(self) -> None:
        root = self.project()
        path = root / "opencode.json"
        original = json.dumps({"theme": "dark", "mcp": {"existing": {"type": "remote"}}})
        path.write_text(
            original,
            encoding="utf-8",
        )
        with patch("joiny_mnemonic.installer.install_hooks"):
            run_setup(
                root,
                agents=("opencode",),
                install_hook_adapters=False,
                install_mcp=True,
                install_plugins=False,
            )
        value = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(value["theme"], "dark")
        self.assertIn("existing", value["mcp"])
        self.assertTrue(value["mcp"]["joiny-mnemonic"]["enabled"])
        backup = path.with_suffix(path.suffix + ".joiny-mnemonic.bak")
        self.assertEqual(backup.read_text(encoding="utf-8"), original)

    def test_interactive_selection_retries_and_separates_activation(self) -> None:
        detections = (
            AgentDetection("claude-code", "Claude Code", "claude", "claude", False),
            AgentDetection("codex", "Codex", "codex", None, False),
        )
        answers = iter(["not-a-number", "1,2", "2,3", "y", "maybe", "y", "bad", "project"])
        output = []
        agents, plugins, with_mcp, scope, enable_extraction = select_interactively(
            detections,
            input_fn=lambda _: next(answers),
            output_fn=output.append,
        )
        self.assertEqual(agents, ("claude-code", "codex"))
        self.assertEqual(plugins, ("knowledge-graph", "nuextract-local"))
        self.assertTrue(enable_extraction)
        self.assertTrue(with_mcp)
        self.assertEqual(scope, "project")
        self.assertTrue(any("Invalid selection" in item for item in output))
        self.assertTrue(any("experimental" in item for item in output))

        parsed = build_parser().parse_args(
            [
                "setup", "--yes", "--agent", "codex", "--plugin",
                "nuextract-local", "--scope", "project", "--enable-extraction",
            ]
        )
        self.assertEqual(parsed.command, "setup")
        self.assertTrue(parsed.enable_extraction)
    def test_bootstrap_scripts_delegate_to_setup(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        powershell = (repository / "install.ps1").read_text(encoding="utf-8")
        shell = (repository / "install.sh").read_text(encoding="utf-8")
        for guarded in (
            '${agents[@]+"${agents[@]}"}',
            '${plugins[@]+"${plugins[@]}"}',
            '${extra[@]+"${extra[@]}"}',
        ):
            self.assertIn(guarded, shell)
        for content in (powershell, shell):
            self.assertIn("setup", content)
            self.assertIn("source-root", content)
            self.assertIn("with-mcp", content.lower())
            self.assertIn("venv", content)
            self.assertIn("enable-extraction", content.lower())
            self.assertIn("revision", content.lower())


if __name__ == "__main__":
    unittest.main()
