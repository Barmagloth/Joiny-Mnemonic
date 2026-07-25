from joiny_mnemonic.storage_support import store_read


class Base:
    @store_read
    def lookup(self):
        pass
