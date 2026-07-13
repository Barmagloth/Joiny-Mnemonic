from __future__ import annotations

import json
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

    def test_configuration_prefers_project_and_enables_nuextract(self) -> None:
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
        self.assertTrue(selected["extractor"]["enabled"])

    def test_service_activates_configured_nuextract_plugin(self) -> None:
        root = self.project()
        write_configuration(
            project_config_path(root),
            {
                "version": 1,
                "scope": "project",
                "agents": [],
                "plugins": ["nuextract-local"],
                "extractor": {"enabled": True, "name": "nuextract-local"},
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
        with MemoryService(
            ":memory:", project_root=root, plugins=registry
        ) as service:
            self.assertTrue(service.extraction.enabled)
            self.assertIs(service.extraction.extractor, extractor)

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

    def test_interactive_selection_and_cli_contract(self) -> None:
        detections = (
            AgentDetection("claude-code", "Claude Code", "claude", "claude", False),
            AgentDetection("codex", "Codex", "codex", None, False),
        )
        answers = iter(["1,2", "2,3", "y", "global"])
        agents, plugins, with_mcp, scope = select_interactively(
            detections, input_fn=lambda _: next(answers), output_fn=lambda _: None
        )
        self.assertEqual(agents, ("claude-code", "codex"))
        self.assertEqual(plugins, ("knowledge-graph", "nuextract-local"))
        self.assertTrue(with_mcp)
        self.assertEqual(scope, "global")

        parsed = build_parser().parse_args(
            [
                "setup", "--yes", "--agent", "codex", "--plugin",
                "knowledge-graph", "--scope", "project", "--with-mcp",
            ]
        )
        self.assertEqual(parsed.command, "setup")
        self.assertEqual(parsed.agent, ["codex"])

    def test_bootstrap_scripts_delegate_to_setup(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        powershell = (repository / "install.ps1").read_text(encoding="utf-8")
        shell = (repository / "install.sh").read_text(encoding="utf-8")
        for content in (powershell, shell):
            self.assertIn("setup", content)
            self.assertIn("source-root", content)
            self.assertIn("with-mcp", content.lower())
            self.assertIn("venv", content)


if __name__ == "__main__":
    unittest.main()
