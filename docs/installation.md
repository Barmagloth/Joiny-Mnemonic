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
- `nuextract-local`: experimental local Transformers/Torch extractor; selecting it only installs
  the backend and does not activate automatic memory writing;
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

For a reproducible source checkout, pin a tag or commit:

```bash
./install.sh --revision <tag-or-commit>
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
- `--enable-extraction`: explicitly bootstrap the experimental policy on a fresh project, or
  append a policy-change request on an existing project; project scope is required;
- `--revision TAG_OR_COMMIT` (`-Revision` in PowerShell): fetch and detach the source checkout at
  an explicit Git tag or commit instead of following the current branch;
- `--dry-run`: show the setup plan without changing project or host configuration;
- `--scope global`: install supported user-global hooks. OpenHands hooks remain project-only.

## Resulting configuration

Project setup writes `.joiny-mnemonic/config.json`; global setup writes
`~/.joiny-mnemonic/config.json` (or `$JOINY_MNEMONIC_HOME/config.json`). Project configuration
takes precedence for installation intent and backend selection only. Its `extractor` object records
`name` and `requested_enabled`; neither field is a runtime trust switch. Services read automatic
enablement only from the active policy ledger.

Project setup also initializes `.joiny-mnemonic/memory.db` once. A repeated setup detects an
existing project identity and does not perform a second trust bootstrap.

With `--enable-extraction`, a fresh bootstrap records the explicit choice in its TOFU policy. For
an existing database, setup appends an idempotent `policy_change_requested` event and leaves
extraction disabled pending trusted approval.

MCP registration uses each installed product's supported surface. Claude Code receives local or
user scope as requested. The current Codex CLI stores MCP servers in user configuration; for a
project setup the registered command still contains the exact project/database path. OpenCode is
merged into `opencode.json`. If a selected product executable is absent, setup reports
`not-installed` rather than silently claiming that MCP is active.

## Safe uninstallation

Run uninstall with the same scope used during setup. The command reads the installer configuration,
removes only Joiny-owned hook handlers and MCP registrations, and removes the installer intent
file. In an interactive terminal it separately asks whether to delete durable project data; the
default is to keep it:

```powershell
& "$HOME\.joiny-mnemonic\runtime\venv\Scripts\python.exe" -m joiny_mnemonic `
  --project-root . uninstall --scope project
```

```bash
"$HOME/.joiny-mnemonic/runtime/venv/bin/python" -m joiny_mnemonic \
  --project-root . uninstall --scope project
```

Use `--dry-run` to inspect the cleanup plan. For non-interactive automation, pass `--keep-data`
(the safe default even when omitted) or the explicit destructive option `--delete-data`. Deletion
covers both current and legacy project databases, SQLite WAL/SHM sidecars, pre-migration database
backups and durable artifacts. It is refused for global scope and deferred if host-integration
cleanup is incomplete.

If the installer configuration is missing, pass one or more explicit `--agent` values.
`--without-hooks` and `--without-mcp` deliberately leave those surfaces untouched. A global
installation must be removed separately with `--scope global`.

Host JSON is edited structurally: unrelated hooks and MCP servers are retained. The OpenCode hook
file is deleted only when it has the generated Joiny signature. If an MCP host executable is not
available, uninstall reports an incomplete cleanup and retains `config.json` so the operation can
be retried instead of silently leaving a dead registration. Because Codex stores project MCP
servers in user configuration, project uninstall also verifies that the live registration still
targets the same project before removing it; an ownership mismatch is left untouched.

With the default keep choice, project `.joiny-mnemonic/memory.db`, artifacts, event history and the
policy ledger remain in place. A later setup at the same project root reopens that database,
preserves `project_instance_id` and history, and applies any required forward schema migrations.
Uninstall does not rewrite the active policy; without hooks or MCP there is no active delivery path.
The external witness registry remains as audit evidence even after an explicit local data deletion.

The runtime under `~/.joiny-mnemonic/runtime` can be shared by multiple projects. Remove that
directory only after uninstalling every project and any global integration, and only after the
uninstall process has exited. A custom `--install-root` must be removed at its corresponding path.

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
