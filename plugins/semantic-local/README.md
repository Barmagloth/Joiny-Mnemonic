# Joiny-Mnemonic local semantic plugin

Persistent cosine-similarity retrieval over both promoted memories and canonical events. The
production encoder is `sentence-transformers/all-MiniLM-L6-v2` by default and is loaded lazily.
The derived vector index is stored at `.joiny-mnemonic/plugins/semantic.sqlite` and may be rebuilt
from the canonical Joiny-Mnemonic store.

```powershell
python -m pip install -e plugins/semantic-local
joiny-mnemonic search "conceptual query without exact keywords"
```

Set `JOINY_MNEMONIC_SEMANTIC_MODEL` to choose another Sentence Transformers model or
`JOINY_MNEMONIC_SEMANTIC_INDEX` to override the derived index path.