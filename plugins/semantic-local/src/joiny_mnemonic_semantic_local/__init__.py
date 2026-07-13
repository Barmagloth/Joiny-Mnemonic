from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import struct
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol

from joiny_mnemonic.models import Event, MemoryRecord, RetrievalHit
from joiny_mnemonic.plugins import PluginContext


class TextEncoder(Protocol):
    def encode(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


class SentenceTransformerEncoder:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model: Any = None

    def encode(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "install joiny-mnemonic-semantic-local with sentence-transformers"
                ) from exc
            self._model = SentenceTransformer(self.model_name)
        return self._model.encode(list(texts), normalize_embeddings=True)


def _fingerprint(*values: str) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(value.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _normalize(vector: Iterable[float]) -> tuple[float, ...]:
    values = tuple(float(value) for value in vector)
    magnitude = math.sqrt(sum(value * value for value in values))
    if not values or magnitude <= 0:
        raise ValueError("semantic encoder returned an empty or zero vector")
    return tuple(value / magnitude for value in values)


def _pack(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(value: bytes, dimension: int) -> tuple[float, ...]:
    return tuple(struct.unpack(f"<{dimension}f", value))


class LocalSemanticRetriever:
    name = "local-sentence-transformers"

    def __init__(self, index_path: str | Path, *, encoder: TextEncoder | None = None) -> None:
        self.index_path = Path(index_path).resolve()
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        model = os.environ.get(
            "JOINY_MNEMONIC_SEMANTIC_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
        )
        self.encoder = encoder or SentenceTransformerEncoder(model)
        self._lock = threading.RLock()
        self._conn = self._open_index()

    def _open_index(self) -> sqlite3.Connection:
        schema = """
            CREATE TABLE IF NOT EXISTS documents (
                source_kind TEXT NOT NULL,
                id TEXT NOT NULL,
                branch_id TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                content TEXT NOT NULL,
                summary TEXT NOT NULL,
                files_json TEXT NOT NULL,
                source_event_ids_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                vector BLOB NOT NULL,
                PRIMARY KEY(source_kind, id)
            );
            CREATE INDEX IF NOT EXISTS idx_semantic_branch ON documents(branch_id, source_kind);
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
    def _memory_document(record: MemoryRecord) -> dict[str, Any]:
        # task5.md B4 (Hindsight's measured cheap win, Apache-2.0): a
        # human-readable date prefix improves temporal awareness of the
        # embedding; asserted valid time wins over admission time.
        date = str(getattr(record, "valid_from", None) or record.created_at)[:10]
        text = "\n".join(
            (f"[Date: {date}]", record.summary, record.content, " ".join(record.files))
        )
        return {
            "source_kind": "memory",
            "id": record.id,
            "branch_id": record.branch_id,
            "memory_type": record.memory_type,
            "content": record.content,
            "summary": record.summary or record.content,
            "files": record.files,
            "source_event_ids": record.source_event_ids,
            "created_at": record.created_at,
            "text": text,
            "fingerprint": _fingerprint(
                record.id, date, record.content, record.summary, *record.files
            ),
        }

    @staticmethod
    def _event_document(event: Event) -> dict[str, Any]:
        text = "\n".join((event.content, " ".join(event.files)))
        return {
            "source_kind": "event",
            "id": event.id,
            "branch_id": event.branch_id,
            "memory_type": event.kind,
            "content": event.content,
            "summary": event.content,
            "files": event.files,
            "source_event_ids": (event.id,),
            "created_at": event.created_at,
            "text": text,
            "fingerprint": _fingerprint(event.id, event.content, *event.files),
        }

    def _index_documents(self, documents: Sequence[dict[str, Any]]) -> None:
        if not documents:
            return
        pending: list[dict[str, Any]] = []
        with self._lock:
            for document in documents:
                row = self._conn.execute(
                    "SELECT fingerprint FROM documents WHERE source_kind=? AND id=?",
                    (document["source_kind"], document["id"]),
                ).fetchone()
                if row is None or row["fingerprint"] != document["fingerprint"]:
                    pending.append(document)
        if not pending:
            return
        vectors = self.encoder.encode([document["text"] for document in pending])
        if len(vectors) != len(pending):
            raise ValueError("semantic encoder returned the wrong vector count")
        with self._lock, self._conn:
            for document, raw_vector in zip(pending, vectors, strict=True):
                vector = _normalize(raw_vector)
                self._conn.execute(
                    "INSERT INTO documents(source_kind,id,branch_id,memory_type,content,summary,"
                    "files_json,source_event_ids_json,created_at,fingerprint,dimension,vector) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_kind,id) DO UPDATE SET "
                    "branch_id=excluded.branch_id,memory_type=excluded.memory_type,"
                    "content=excluded.content,summary=excluded.summary,files_json=excluded.files_json,"
                    "source_event_ids_json=excluded.source_event_ids_json,created_at=excluded.created_at,"
                    "fingerprint=excluded.fingerprint,dimension=excluded.dimension,vector=excluded.vector",
                    (
                        document["source_kind"], document["id"], document["branch_id"],
                        document["memory_type"], document["content"], document["summary"],
                        json.dumps(document["files"], ensure_ascii=False),
                        json.dumps(document["source_event_ids"], ensure_ascii=False),
                        document["created_at"], document["fingerprint"], len(vector), _pack(vector),
                    ),
                )

    def index(self, record: MemoryRecord) -> None:
        self._index_documents((self._memory_document(record),))

    def index_event(self, event: Event) -> None:
        self._index_documents((self._event_document(event),))

    def sync(
        self,
        records: Sequence[MemoryRecord],
        events: Sequence[Event] = (),
    ) -> None:
        documents = [self._memory_document(record) for record in records]
        documents.extend(self._event_document(event) for event in events)
        self._index_documents(documents)

    def search(
        self, query: str, *, limit: int, filters: dict[str, Any]
    ) -> list[RetrievalHit]:
        if not query.strip() or limit < 1:
            return []
        query_vector = _normalize(self.encoder.encode((query,))[0])
        allowed_memory_ids = set(filters.get("allowed_memory_ids") or ())
        allowed_event_ids = set(filters.get("allowed_event_ids") or ())
        restrict_ids = "allowed_memory_ids" in filters or "allowed_event_ids" in filters
        with self._lock:
            rows = self._conn.execute("SELECT * FROM documents").fetchall()
        hits: list[RetrievalHit] = []
        for row in rows:
            if restrict_ids:
                allowed = allowed_memory_ids if row["source_kind"] == "memory" else allowed_event_ids
                if row["id"] not in allowed:
                    continue
            elif row["branch_id"] != filters.get("branch_id", row["branch_id"]):
                continue
            vector = _unpack(bytes(row["vector"]), int(row["dimension"]))
            if len(vector) != len(query_vector):
                continue
            score = max(0.0, min(1.0, sum(a * b for a, b in zip(query_vector, vector))))
            if score <= 0:
                continue
            hits.append(
                RetrievalHit(
                    id=row["id"],
                    source_kind=row["source_kind"],
                    memory_type=row["memory_type"],
                    representation="semantic",
                    content=row["summary"] if row["source_kind"] == "memory" else row["content"],
                    score=score,
                    source_event_ids=tuple(json.loads(row["source_event_ids_json"])),
                    files=tuple(json.loads(row["files_json"])),
                    created_at=row["created_at"],
                    metadata={
                        "plugin": self.name,
                        "retrieval_backend": "sentence-transformers-cosine",
                        "index_path": str(self.index_path),
                    },
                )
            )
        return sorted(hits, key=lambda hit: (hit.score, hit.created_at), reverse=True)[:limit]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def create_plugin(context: PluginContext | None = None) -> LocalSemanticRetriever:
    configured = os.environ.get("JOINY_MNEMONIC_SEMANTIC_INDEX")
    if configured:
        index_path = Path(configured)
    elif context is not None:
        index_path = context.project_root / ".joiny-mnemonic" / "plugins" / "semantic.sqlite"
    else:
        index_path = Path.cwd() / ".joiny-mnemonic" / "plugins" / "semantic.sqlite"
    return LocalSemanticRetriever(index_path)