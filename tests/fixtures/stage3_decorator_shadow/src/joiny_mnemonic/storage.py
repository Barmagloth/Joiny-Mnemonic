from joiny_mnemonic.storage_support import store_read

store_read = lambda function: function


class MemoryStore:
    @store_read
    def future_write(self):
        pass
