# Backup, restore, and retention

Required backup data consists of canonical events and artifacts, the immutable interpretation
ledger, policy ledger, security findings/transitions, candidate-memory lineage and prompt exposure
records. Restore must preserve project_instance_id, chain_id, bootstrap metadata and all event
hashes.

Semantic/FTS indexes, graph state, current candidate status, backlog/resume projections and other
derived views may be rebuilt. The interpretation ledger is not a rebuildable cache.

Snapshot state blobs are derived and may be pruned only through a canonical `snapshots_pruned`
event. Snapshot rows, lineage, `state_sha256`, replay code version and pruning records remain
permanent. Prompt-exposed snapshots and snapshots referenced by active tasks are protected.

Keep candidate and attempt lineage for at least as long as any linked memory or recorded prompt
exposure. Never prune accepted, confirmed, superseded or exposed lineage independently. Unlinked,
never-exposed rejected candidates may be archived only under an explicit retention policy.
Compressed redacted raw responses may have a shorter documented retention period, but structured
candidates and lineage outlive them.

A legitimate restore/import that starts a new chain must declare a new chain_id, preserve linkage
to the prior chain and emit a visible event/finding. It is not equivalent to a normal extension.
The witness registry is backed up independently if rollback detection should survive loss of the
workspace database. It is per-project sharded: back up the `witnesses.d/` directory next to
`witnesses.json` (the legacy monolith remains a read-only migration fallback; shards hold the
current witnessed heads).
## Schema upgrades

The durable database carries an integer `metadata.schema_version` and an immutable
`schema_migrations` ledger. Opening code rejects a database whose version is newer than it
supports before running schema DDL. Older supported databases are upgraded only forward; each
versioned step records its source version, application time, code version and backup path.

Before changing any existing on-disk schema, the store creates a SQLite online backup next to the
database using the `memory.db.pre-migration-vN-to-vM-*.bak` naming pattern and verifies it with
`PRAGMA integrity_check`. A failed migration leaves this backup available and does not advance the
schema-version record. These safety backups contain the same sensitive information as the primary
database and are included by explicit uninstall `--delete-data`; normal uninstall preserves them.

New releases must add a new ordered migration function and increment `CURRENT_SCHEMA_VERSION`.
They must not reinterpret an existing schema version in place. Migration tests must cover upgrade,
reopen idempotency, backup integrity and fail-closed handling of future versions.
