# NuExtract local extractor

This optional plugin keeps ML dependencies out of the joiny-mnemonic core. Installing it only
provisions a backend; it does not activate automatic memory writing.

Runtime activation is controlled exclusively by `automatic_extraction_enabled` in the active
immutable policy ledger. On a fresh project, an explicit `joiny-mnemonic setup --enable-extraction`
choice may enter the initial TOFU policy. On an existing project the same option only appends a
trusted-policy change request. Mutable environment variables and workspace configuration cannot
activate extraction.

The default model is `numind/NuExtract-1.5`. Set `JOINY_MNEMONIC_NUEXTRACT_MODEL` and
`JOINY_MNEMONIC_NUEXTRACT_REVISION` to a pinned model and revision. Production evaluation reports
must include that revision in the extractor configuration hash. `JOINY_MNEMONIC_NUEXTRACT_MAX_TOKENS`
controls the bounded generation limit but is not an enablement switch.

Only redacted canonical message text is passed by the core. Context is bounded and read-only;
evidence quotes must still occur exactly once in the current event. Automatic enablement remains
gated on reviewed RU/EN real-model reports with zero false trusted records and one live host E2E
session.