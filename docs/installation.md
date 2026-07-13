# Installation and setup

Joiny-Mnemonic has one agent-neutral Python core. Host-specific behavior is limited to small
adapters selected by `joiny-mnemonic setup`; the bootstrap scripts only create a stable Python
runtime and delegate to that command.

## Guided installation

Python 3.11+ and Git are required. From a checkout:

```powershell
git clone https://github.com/Barmagloth/Joiny-Mnemonic.git
cd Joiny-Mnemonic
.\install.ps1
```

```bash
git clone https://github.com/Barmagloth/Joiny-Mnemonic.git
cd Joiny-Mnemonic
./install.sh
```

The scripts create `~/.joiny-mnemonic/runtime/venv`, install the core there, and launch the same
guided setup on every platform. The generated hooks use the venv's exact Python executable, so
they do not depend on the shell's later `PATH` or on a disposable project virtual environment.

The wizard detects Claude Code, Codex, OpenCode and OpenHands from executables and existing
configuration. Detected products are preselected. It then asks for optional components:

- `semantic-local`: local sentence-transformer retrieval; downloads model weights on first use;
- `knowledge-graph`: lightweight local SQLite graph projection;
- `nuextract-local`: local Transformers/Torch extractor; largest download and disabled unless
  selected;
- MCP registration: optional and independent of automatic hook capture.

The default scope is project-local, optional components are unchecked, and MCP registration is
off. Re-running setup is supported. Existing host JSON is validated; OpenCode MCP configuration
and hook-owned JSON are backed up before replacement.

## Non-interactive installation

Selections use stable, vendor-neutral identifiers:

```powershell
.\install.ps1 -Yes -Scope project `
  -Agent claude-code,codex `
  -Plugin knowledge-graph,nuextract-local `
  -WithMcp
```

```bash
./install.sh --yes --scope project \
  --agent claude-code --agent codex \
  --plugin knowledge-graph --plugin nuextract-local \
  --with-mcp
```

If the core is already installed, call it directly:

```powershell
joiny-mnemonic --project-root . setup --yes `
  --agent claude-code --agent codex `
  --plugin knowledge-graph --plugin nuextract-local --with-mcp
```

Useful controls:

- `--all-plugins`: install all three bundled optional components;
- `--without-hooks`: configure components/MCP without automatic host capture;
- `--skip-plugin-install`: record a configuration when components were provisioned separately;
- `--dry-run`: show the setup plan without changing project or host configuration;
- `--scope global`: install supported user-global hooks. OpenHands hooks remain project-only.

## Resulting configuration

Project setup writes `.joiny-mnemonic/config.json`; global setup writes
`~/.joiny-mnemonic/config.json` (or `$JOINY_MNEMONIC_HOME/config.json`). Project configuration
takes precedence. Selecting NuExtract records its explicit enablement, and services/hooks load
that setting on their next process start. Merely installing the package does not enable it.

Project setup also initializes `.joiny-mnemonic/memory.db` once. A repeated setup detects an
existing project identity and does not perform a second trust bootstrap.

MCP registration uses each installed product's supported surface. Claude Code receives local or
user scope as requested. The current Codex CLI stores MCP servers in user configuration; for a
project setup the registered command still contains the exact project/database path. OpenCode is
merged into `opencode.json`. If a selected product executable is absent, setup reports
`not-installed` rather than silently claiming that MCP is active.

## Manual fallback

The lower-level commands remain supported:

```powershell
python -m pip install .
joiny-mnemonic --project-root . install-hooks claude-code
joiny-mnemonic --project-root . install-hooks codex
python -m pip install plugins/knowledge-graph
```

Use `joiny-mnemonic capabilities` and the product's `mcp list/get` command to verify the final
host state.
