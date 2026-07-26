from __future__ import annotations

import unittest

from joiny_mnemonic.projection_storage import ProjectionStorageMixin
from joiny_mnemonic.storage import (
    MemoryStore,
    SchemaCompatibilityError,
    SnapshotIntegrityError,
    StoreIntegrityError,
)
from joiny_mnemonic.storage_errors import (
    SchemaCompatibilityError as SplitSchemaCompatibilityError,
)
from joiny_mnemonic.storage_errors import (
    SnapshotIntegrityError as SplitSnapshotIntegrityError,
)
from joiny_mnemonic.storage_errors import StoreIntegrityError as SplitStoreIntegrityError


class Stage3StorageSplitTest(unittest.TestCase):
    def test_projection_mixin_owns_rebuildable_projection_storage(self) -> None:
        for name in (
            "retrieval_health_load",
            "retrieval_health_store",
            "file_hash_cache_load",
            "file_hash_cache_store",
        ):
            self.assertNotIn(name, MemoryStore.__dict__)
            self.assertIn(name, ProjectionStorageMixin.__dict__)

        with MemoryStore(":memory:") as store:
            store.retrieval_health_store(
                {"semantic:test": {"status": "healthy", "watermark": 7}}
            )
            self.assertEqual(
                store.retrieval_health_load(),
                {"semantic:test": {"status": "healthy", "watermark": 7}},
            )
            store.file_hash_cache_store(
                "R:/project", {"README.md": (12, 34, "abc123")}
            )
            self.assertEqual(
                store.file_hash_cache_load("R:/project"),
                {"README.md": (12, 34, "abc123")},
            )

    def test_storage_error_imports_remain_backward_compatible(self) -> None:
        self.assertIs(StoreIntegrityError, SplitStoreIntegrityError)
        self.assertIs(SchemaCompatibilityError, SplitSchemaCompatibilityError)
        self.assertIs(SnapshotIntegrityError, SplitSnapshotIntegrityError)


if __name__ == "__main__":
    unittest.main()
