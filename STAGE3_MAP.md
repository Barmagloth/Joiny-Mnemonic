# Stage 3 mutation-path map

Roadmap authority: `ROADMAP.md` section 6. This file is an implementation
inventory, not a competing specification.

## Step 1 baseline

Executable inventory: `python scripts/stage3_surface_audit.py`.

The audit is fail-closed: every direct `.store.<method>(...)` call in CLI, MCP,
HTTP or hooks is treated as a write unless the MemoryStore declaration carries
the canonical `@store_read` ownership marker. Raw-store aliases, bound methods,
`getattr`, subscripts and passing the store to another function are violations.
`--require-clean` is the final Stage 3 acceptance mode.

Current direct write ownership violations:

| Surface | Direct writes to move behind the application layer |
|---|---|
| CLI | `record_security_finding`, `start_session`, `create_branch`, `append_artifact`, `set_active_block`, `set_budget_policy` |
| MCP | `set_active_block` |
| HTTP | `start_session`, `create_branch`, `append_artifact`, `set_active_block`, `set_budget_policy` |
| hooks | `hook_session`, `bind_task_session`, `append_host_events_once`, `after_commit` |

Read-only calls remain permitted because Stage 3 forbids direct *mutating*
store calls, not queries. Classification is derived from store declarations,
not a second allowlist in the gate; unknown methods and store escapes are never
silently classified as reads.

## Planned ownership

- Public surfaces parse/serialize only and call `MemoryService` application
  commands.
- The application layer owns trust, transition legality, obligation checks,
  orchestration and transaction boundaries.
- Storage modules own SQLite, SQL and row conversion only.
- Existing CLI/MCP/HTTP response formats remain unchanged.
- The inventory must reach zero before `JM-INV-003` can pass.
