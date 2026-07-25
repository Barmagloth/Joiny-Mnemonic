from evil.storage_support import store_read


class MemoryStore:
    @store_read
    def future_write(self):
        pass
