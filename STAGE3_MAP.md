# Stage 3 mutation-path map

Roadmap authority: `ROADMAP.md` section 6. This file is an implementation
inventory, not a competing specification.

## Step 1 baseline

Executable inventory: `python scripts/stage3_surface_audit.py`.

The audit is deliberately bounded to the structural rule in Roadmap section 6:
every direct `.store.<method>(...)` call in CLI, MCP, HTTP or hooks is treated
as a write unless the effective runtime `MemoryStore` method carries the
canonical `@store_read` marker. A direct raw-store capability escape is also a
violation. The gate is not a general Python data-flow or metaprogramming
analyser; JM-INV-003 is proved separately by a behavioural test through all
public surfaces. `--require-clean` is the final structural acceptance mode.

Step 1 baseline direct write ownership violations:

| Surface | Direct writes to move behind the application layer |
|---|---|
| CLI | `record_security_finding`, `start_session`, `create_branch`, `append_artifact`, `set_active_block`, `set_budget_policy` |
| MCP | `set_active_block` |
| HTTP | `start_session`, `create_branch`, `append_artifact`, `set_active_block`, `set_budget_policy` |
| hooks | `hook_session`, `bind_task_session`, `append_host_events_once`, `after_commit` |

Read-only calls remain permitted because Stage 3 forbids direct *mutating*
store calls, not queries. Classification is derived from the effective runtime
class (including inherited methods), not a second allowlist in the gate.
Unknown direct methods and direct store escapes are not classified as reads.

## Step 2 application command path

`ApplicationCommands` in `src/joiny_mnemonic/application.py` is the named
mutation boundary. CLI, MCP and HTTP now invoke it instead of calling mutating
`MemoryStore` methods directly. Their existing result objects and serialized
response formats are preserved.

Executable behavioural proof:

```powershell
$env:PYTHONPATH='src'
python -m unittest tests.test_stage3_application_commands.ApplicationCommandsTest.test_jm_inv_003_all_public_surfaces_share_application_path -v
```

That test performs the same active-block operation through CLI, MCP and HTTP,
checks the successful result on every surface, and proves that the canonical
stored-size validation rejects the same oversized input on all three.

After this migration, the bounded audit contained only four hook writes,
reserved for the next independent step:

| Surface | Remaining direct writes |
|---|---|
| hooks | `hook_session`, `bind_task_session`, `append_host_events_once`, `after_commit` |

## Step 3 hook command path

Host hooks now use the same `ApplicationCommands` boundary. Session resolution
and optional Workstream binding are one application operation; idempotent host
event append and after-commit scheduling are also exposed as named commands.
The structural inventory is therefore empty.

Executable proof:

```powershell
$env:PYTHONPATH='src'
python -m unittest tests.test_stage3_application_commands.ApplicationCommandsTest.test_jm_inv_003_all_public_surfaces_share_application_path -v
python scripts/stage3_surface_audit.py --require-clean
```

## Target ownership

- Public surfaces parse/serialize only and call `MemoryService` application
  commands.
- The application layer owns trust, transition legality, obligation checks,
  orchestration and transaction boundaries.
- Storage modules own SQLite, SQL and row conversion only.
- Existing CLI/MCP/HTTP response formats remain unchanged.
- The inventory must reach zero before final Stage 3 acceptance.

## Step 4 storage split

`ProjectionStorageMixin` in `src/joiny_mnemonic/projection_storage.py` is the
single SQLite owner for the rebuildable retrieval-health and file-hash
projections. It uses the parent `MemoryStore` connection and transaction; no
database or service boundary was added. `src/joiny_mnemonic/storage_errors.py`
owns the three storage exception types, while `storage.py` re-exports them for
backward-compatible imports.

The lowered limits in `quality/complexity-baseline.json` are exact and cannot
be regenerated upward by the gate:

| Module | Original Stage 1 baseline | Enforced after split |
|---|---:|---:|
| `storage.py` physical lines | 4847 | 4702 |
| `storage.py` functions/methods | 156 | 138 |
| `storage.py` classes | 4 | 1 |

Both new modules are independently capped at their current metrics as well as
by the project-wide 1000-line limit.

Executable proof:

```powershell
$env:PYTHONPATH='src'
python -m unittest tests.test_stage3_storage_split -v
python scripts/stage1_gates.py complexity
```
