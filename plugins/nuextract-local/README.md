# NuExtract local extractor

This optional plugin keeps ML dependencies out of the joiny-mnemonic core. Install it
separately, then opt in with JOINY_MNEMONIC_EXTRACTOR_ENABLED=1.

The default model is numind/NuExtract-1.5. Set
JOINY_MNEMONIC_NUEXTRACT_MODEL to a pinned local path or another compatible
model. Production deployments should pin weights/revision and include that revision in
the extractor configuration hash.

Only redacted canonical message text is passed by the core. Context is bounded and
read-only; evidence quotes must still occur exactly once in the current event.