from joiny_mnemonic.storage_support import store_read


class MemoryStore:
    @store_read
    def stale_read(self):
        pass


from evil import MemoryStore
