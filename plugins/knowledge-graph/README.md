# Joiny-Mnemonic knowledge graph plugin

A project-local SQLite projection over explicit entities and relations in promoted memories. Every
edge retains the originating memory and canonical event IDs; the graph is disposable and can be
rebuilt from the authoritative Joiny-Mnemonic store.

Use `[[Entity]] -[relation]-> [[Other Entity]]` for precise edges. Backticked/file/code entities
and natural relation verbs such as `uses`, `depends on`, `requires`, `calls`, `reads from`, and
`writes to` are also recognized. Co-occurring explicit entities receive a lower-confidence
`co_occurs` edge.

```powershell
python -m pip install -e plugins/knowledge-graph
joiny-mnemonic graph-neighbors "Entity"
```

Set `JOINY_MNEMONIC_GRAPH_INDEX` to override the default derived index path
`.joiny-mnemonic/plugins/knowledge-graph.sqlite`.