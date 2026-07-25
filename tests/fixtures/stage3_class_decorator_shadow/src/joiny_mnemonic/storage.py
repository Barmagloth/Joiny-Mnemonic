from joiny_mnemonic.storage_support import store_read


class MemoryStore:
    store_read = lambda function: function

    @store_read
    def future_write(self):
        pass
