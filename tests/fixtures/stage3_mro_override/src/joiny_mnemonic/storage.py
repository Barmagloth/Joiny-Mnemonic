from . import other
from .base import Base


class MemoryStore(Base):
    def future_write(self):
        pass

    @other.store_read
    def bogus_read(self):
        pass
