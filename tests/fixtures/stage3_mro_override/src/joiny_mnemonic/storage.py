from . import other
from .base import Base
from joiny_mnemonic.storage_support import store_read


class MemoryStore(Base):
    future_write = object()

    @store_read
    def duplicate(self):
        pass

    def duplicate(self):
        pass

    @other.store_read
    def bogus_read(self):
        pass
