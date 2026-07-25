from .base import Base
from joiny_mnemonic.storage_support import store_read


class MemoryStore(Base):
    @store_read
    def lookup(self):
        pass

    del lookup
