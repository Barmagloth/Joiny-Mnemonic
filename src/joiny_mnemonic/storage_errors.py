class StoreIntegrityError(RuntimeError):
    """Raised when canonical storage fails automatic integrity verification."""


class SchemaCompatibilityError(RuntimeError):
    """Raised before mutation when the database schema is unsupported or malformed."""


class SnapshotIntegrityError(RuntimeError):
    """Raised internally when materialized snapshot state fails hash verification."""

    def __init__(self, snapshot_id: str, expected: str, actual: str) -> None:
        self.snapshot_id = snapshot_id
        self.expected = expected
        self.actual = actual
        super().__init__(f"snapshot {snapshot_id} state hash mismatch")
