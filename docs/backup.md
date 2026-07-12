# Backup, restore, and retention

Required backup data consists of canonical events and artifacts, the immutable interpretation
ledger, policy ledger, security findings/transitions, candidate-memory lineage and prompt exposure
records. Restore must preserve project_instance_id, chain_id, bootstrap metadata and all event
hashes.

Semantic/FTS indexes, graph state, current candidate status, backlog/resume projections and other
derived views may be rebuilt. The interpretation ledger is not a rebuildable cache.

Keep candidate and attempt lineage for at least as long as any linked memory or recorded prompt
exposure. Never prune accepted, confirmed, superseded or exposed lineage independently. Unlinked,
never-exposed rejected candidates may be archived only under an explicit retention policy.
Compressed redacted raw responses may have a shorter documented retention period, but structured
candidates and lineage outlive them.

A legitimate restore/import that starts a new chain must declare a new chain_id, preserve linkage
to the prior chain and emit a visible event/finding. It is not equivalent to a normal extension.
The witness registry is backed up independently if rollback detection should survive loss of the
workspace database.