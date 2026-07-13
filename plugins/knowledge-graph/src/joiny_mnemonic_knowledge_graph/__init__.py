from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from itertools import combinations
from pathlib import Path
from typing import Any, Sequence

from joiny_mnemonic.models import MemoryRecord, RetrievalHit
from joiny_mnemonic.plugins import PluginContext


EXPLICIT_RELATION = re.compile(
    r"\[\[([^\]]+)\]\]\s*-\[([^\]]+)\]->\s*\[\[([^\]]+)\]\]",
    re.IGNORECASE,
)
ENTITY_TOKEN = r"(?:\[\[([^\]]+)\]\]|`([^`]+)` )"
NATURAL_RELATION = re.compile(
    r"(?:\[\[([^\]]+)\]\]|`([^`]+)`)\s+"
    r"(uses|depends\s+on|requires|calls|implements|owns|blocks|supersedes|"
    r"reads\s+from|writes\s+to)\s+"
    r"(?:\[\[([^\]]+)\]\]|`([^`]+)`)",
    re.IGNORECASE,
)
MARKED_ENTITY = re.compile(r"\[\[([^\]]+)\]\]")
BACKTICK_ENTITY = re.compile(r"`([^`\n]{2,160})`")
FILE_ENTITY = re.compile(r"(?<![\w])(?:[A-Za-z]:[\\/])?[\w.-]+(?:[\\/][\w .-]+)+")
CODE_ENTITY = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b")


def _clean(value: str) -> str:
    return " ".join(value.strip().strip("`[]").split())[:240]


def _key(value: str) -> str:
    return _clean(value).casefold()


def _fingerprint(record: MemoryRecord) -> str:
    digest = hashlib.sha256()
    for value in (record.id, record.content, record.summary, *record.files):
        digest.update(value.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _edge_id(memory_id: str, source: str, relation: str, target: str) -> str:
    value = f"{memory_id}\x00{_key(source)}\x00{relation.casefold()}\x00{_key(target)}"
    return "edge_" + hashlib.sha256(value.encode()).hexdigest()[:32]


class SQLiteKnowledgeGraph:
    name = "sqlite-entity-graph"

    def __init__(self, index_path: str | Path) -> None:
        self.index_path = Path(index_path).resolve()
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = self._open_index()

    def _open_index(self) -> sqlite3.Connection:
        schema = """
            CREATE TABLE IF NOT EXISTS memory_projection (
                memory_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS edges (
                id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                branch_id TEXT NOT NULL,
                source_entity TEXT NOT NULL,
                source_key TEXT NOT NULL,
                relation TEXT NOT NULL,
                target_entity TEXT NOT NULL,
                target_key TEXT NOT NULL,
                confidence REAL NOT NULL,
                memory_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                files_json TEXT NOT NULL,
                source_event_ids_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_key, created_at);
            CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_key, created_at);
            CREATE INDEX IF NOT EXISTS idx_edges_memory ON edges(memory_id);
        """

        def connect(*, exclusive: bool) -> sqlite3.Connection:
            connection = sqlite3.connect(str(self.index_path), check_same_thread=False)
            connection.row_factory = sqlite3.Row
            try:
                if exclusive:
                    connection.execute("PRAGMA locking_mode=EXCLUSIVE")
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(schema)
                connection.execute("PRAGMA busy_timeout=5000")
                return connection
            except Exception:
                connection.close()
                raise

        try:
            return connect(exclusive=False)
        except sqlite3.OperationalError:
            return connect(exclusive=True)

    @staticmethod
    def _entities(record: MemoryRecord) -> list[str]:
        values: list[str] = []
        text = f"{record.content}\n{record.summary}"
        for pattern in (MARKED_ENTITY, BACKTICK_ENTITY, FILE_ENTITY, CODE_ENTITY):
            for match in pattern.finditer(text):
                value = _clean(match.group(1) if match.lastindex else match.group(0))
                if value and _key(value) not in {_key(item) for item in values}:
                    values.append(value)
        for file in record.files:
            value = _clean(file)
            if value and _key(value) not in {_key(item) for item in values}:
                values.append(value)
        return values[:24]

    @classmethod
    def _relations(cls, record: MemoryRecord) -> list[tuple[str, str, str, float]]:
        text = f"{record.content}\n{record.summary}"
        relations: list[tuple[str, str, str, float]] = []
        related_pairs: set[frozenset[str]] = set()
        for match in EXPLICIT_RELATION.finditer(text):
            source, relation, target = (_clean(item) for item in match.groups())
            if source and relation and target and _key(source) != _key(target):
                relations.append((source, relation.casefold().replace(" ", "_"), target, 1.0))
                related_pairs.add(frozenset((_key(source), _key(target))))
        for match in NATURAL_RELATION.finditer(text):
            source = _clean(match.group(1) or match.group(2) or "")
            relation = _clean(match.group(3)).casefold().replace(" ", "_")
            target = _clean(match.group(4) or match.group(5) or "")
            if source and target and _key(source) != _key(target):
                relations.append((source, relation, target, 0.9))
                related_pairs.add(frozenset((_key(source), _key(target))))
        entities = cls._entities(record)[:12]
        for source, target in combinations(entities, 2):
            pair = frozenset((_key(source), _key(target)))
            if pair not in related_pairs:
                relations.append((source, "co_occurs", target, 0.55))
        deduplicated: dict[tuple[str, str, str], tuple[str, str, str, float]] = {}
        for source, relation, target, confidence in relations:
            key = (_key(source), relation, _key(target))
            current = deduplicated.get(key)
            if current is None or current[3] < confidence:
                deduplicated[key] = (source, relation, target, confidence)
        return list(deduplicated.values())

    def project(self, record: MemoryRecord) -> None:
        fingerprint = _fingerprint(record)
        with self._lock:
            row = self._conn.execute(
                "SELECT fingerprint FROM memory_projection WHERE memory_id=?", (record.id,)
            ).fetchone()
            if row is not None and row["fingerprint"] == fingerprint:
                return
        relations = self._relations(record)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM edges WHERE memory_id=?", (record.id,))
            for source, relation, target, confidence in relations:
                self._conn.execute(
                    "INSERT INTO edges(id,memory_id,branch_id,source_entity,source_key,relation,"
                    "target_entity,target_key,confidence,memory_type,summary,files_json,"
                    "source_event_ids_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        _edge_id(record.id, source, relation, target), record.id, record.branch_id,
                        source, _key(source), relation, target, _key(target), confidence,
                        record.memory_type, record.summary or record.content,
                        json.dumps(record.files, ensure_ascii=False),
                        json.dumps(record.source_event_ids, ensure_ascii=False), record.created_at,
                    ),
                )
            self._conn.execute(
                "INSERT INTO memory_projection(memory_id,fingerprint) VALUES(?,?) "
                "ON CONFLICT(memory_id) DO UPDATE SET fingerprint=excluded.fingerprint",
                (record.id, fingerprint),
            )

    def sync(self, records: Sequence[MemoryRecord]) -> None:
        for record in records:
            self.project(record)

    _CAUSAL = {"causes", "caused_by", "enables", "prevents"}
    _ARM_FANOUT_CAP = 200  # per-entity, Hindsight's shipped default

    def search_arm(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievalHit]:
        """Graph retrieval arm (task5.md B5; scoring recipe from Hindsight,
        Apache-2.0): convergent evidence accumulates —
        tanh(0.5*shared_entities) + best link confidence + causal bonus.
        Hits are keyed by memory_id so RRF merges them with the base arm."""
        import math
        import re as _re

        tokens = set(_re.findall(r"`([^`\n]+)`", query))
        tokens.update(_re.findall(r"[\w.-]{3,}", query, _re.UNICODE))
        keys = {key for key in (_key(token) for token in tokens) if key}
        if not keys or limit < 1:
            return []
        allowed_ids = set((filters or {}).get("allowed_memory_ids") or ())
        restrict = filters is not None and "allowed_memory_ids" in filters
        aggregate: dict[str, dict[str, Any]] = {}
        with self._lock:
            for key in keys:
                rows = self._conn.execute(
                    "SELECT * FROM edges WHERE source_key=? OR target_key=? "
                    "ORDER BY confidence DESC LIMIT ?",
                    (key, key, self._ARM_FANOUT_CAP),
                ).fetchall()
                for row in rows:
                    memory_id = row["memory_id"]
                    if restrict and memory_id not in allowed_ids:
                        continue
                    entry = aggregate.setdefault(
                        memory_id,
                        {"keys": set(), "confidence": 0.0, "causal": 0.0, "row": row},
                    )
                    entry["keys"].add(key)
                    entry["confidence"] = max(
                        entry["confidence"], float(row["confidence"])
                    )
                    if row["relation"] in self._CAUSAL:
                        entry["causal"] = max(
                            entry["causal"], float(row["confidence"]) + 1.0
                        )
        hits: list[RetrievalHit] = []
        for memory_id, entry in aggregate.items():
            row = entry["row"]
            score = (
                math.tanh(0.5 * len(entry["keys"]))
                + entry["confidence"]
                + entry["causal"]
            )
            hits.append(
                RetrievalHit(
                    id=memory_id,
                    source_kind="memory",
                    memory_type=row["memory_type"],
                    representation="graph-arm",
                    content=row["summary"],
                    score=score,
                    source_event_ids=tuple(json.loads(row["source_event_ids_json"])),
                    files=tuple(json.loads(row["files_json"])),
                    created_at=row["created_at"],
                    metadata={
                        "plugin": self.name,
                        "graph_arm": {
                            "matched_entities": sorted(entry["keys"]),
                            "best_confidence": entry["confidence"],
                            "causal_bonus": entry["causal"],
                        },
                    },
                )
            )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]

    def neighbors(
        self,
        entity: str,
        *,
        limit: int,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievalHit]:
        entity_key = _key(entity)
        if not entity_key or limit < 1:
            return []
        allowed_ids = set((filters or {}).get("allowed_memory_ids") or ())
        restrict_ids = filters is not None and "allowed_memory_ids" in filters
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM edges WHERE source_key=? OR target_key=? "
                "ORDER BY confidence DESC, created_at DESC",
                (entity_key, entity_key),
            ).fetchall()
        hits: list[RetrievalHit] = []
        for row in rows:
            if restrict_ids and row["memory_id"] not in allowed_ids:
                continue
            content = (
                f"{row['source_entity']} -[{row['relation']}]-> {row['target_entity']}\n"
                f"{row['summary']}"
            )
            hits.append(
                RetrievalHit(
                    id=row["id"],
                    source_kind="knowledge_graph",
                    memory_type=row["memory_type"],
                    representation="edge",
                    content=content,
                    score=float(row["confidence"]),
                    source_event_ids=tuple(json.loads(row["source_event_ids_json"])),
                    files=tuple(json.loads(row["files_json"])),
                    created_at=row["created_at"],
                    metadata={
                        "plugin": self.name,
                        "memory_id": row["memory_id"],
                        "source_entity": row["source_entity"],
                        "relation": row["relation"],
                        "target_entity": row["target_entity"],
                        "index_path": str(self.index_path),
                    },
                )
            )
            if len(hits) >= limit:
                break
        return hits

    def resolve_source_ids(self, identifier: str) -> tuple[str, ...] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT source_event_ids_json FROM edges WHERE id=?", (identifier,)
            ).fetchone()
        if row is None:
            return None
        return tuple(json.loads(row["source_event_ids_json"]))

    def resolve_branch_id(self, identifier: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT branch_id FROM edges WHERE id=?", (identifier,)
            ).fetchone()
        return str(row["branch_id"]) if row is not None else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def create_plugin(context: PluginContext | None = None) -> SQLiteKnowledgeGraph:
    configured = os.environ.get("JOINY_MNEMONIC_GRAPH_INDEX")
    if configured:
        index_path = Path(configured)
    elif context is not None:
        index_path = (
            context.project_root / ".joiny-mnemonic" / "plugins" / "knowledge-graph.sqlite"
        )
    else:
        index_path = Path.cwd() / ".joiny-mnemonic" / "plugins" / "knowledge-graph.sqlite"
    return SQLiteKnowledgeGraph(index_path)