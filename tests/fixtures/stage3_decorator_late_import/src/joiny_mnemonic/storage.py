def store_read(function):
    return function


class MemoryStore:
    @store_read
    def future_write(self):
        pass


from joiny_mnemonic.storage_support import store_read
