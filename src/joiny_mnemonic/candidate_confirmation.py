from __future__ import annotations

import uuid

from .storage_support import atomic_write, integrity_checked, now


class CandidateConfirmationMixin:
    @integrity_checked
    def find_auto_candidate_match(
        self, memory_type: str, content: str
    ) -> tuple[str, str | None] | None:
        key = self._normalized_key(content)
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.id, c.normalized_content, l.memory_id, "
                "(SELECT t.to_status FROM candidate_transitions t "
                "WHERE t.candidate_id=c.id ORDER BY t.rowid DESC LIMIT 1) AS status "
                "FROM extraction_candidates c "
                "LEFT JOIN candidate_memory_links l ON l.candidate_id=c.id "
                "WHERE c.memory_type=? ORDER BY c.created_at",
                (memory_type,),
            ).fetchall()
        for row in rows:
            if row["status"] in {
                "auto", "quarantined", "confirmation_requested", "confirmed"
            } and self._normalized_key(row["normalized_content"]) == key:
                memory_id = row["memory_id"]
                return str(row["id"]), str(memory_id) if memory_id else None
        return None

    @atomic_write
    def confirm_candidate_match(
        self,
        candidate_id: str,
        memory_id: str | None,
        *,
        source_event_id: str,
    ) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT c.*, r.event_id AS candidate_source_event_id "
                "FROM extraction_candidates c JOIN extraction_runs r ON r.id=c.run_id "
                "WHERE c.id=?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown extraction candidate: {candidate_id}")
        current = self._candidate_status_locked(self._conn, candidate_id)
        if current in {"auto", "quarantined"}:
            self.transition_candidate(
                candidate_id,
                "confirmation_requested",
                source_event_id=source_event_id,
                actor="explicit_marker",
                rule_id="normalized_explicit_match_requested",
            )
        self.transition_candidate(
            candidate_id,
            "confirmed",
            source_event_id=source_event_id,
            actor="explicit_marker",
            rule_id="normalized_explicit_match",
        )
        if memory_id is None:
            candidate_source = self.get_event(str(row["candidate_source_event_id"]))
            record = self.derive_memory(
                memory_type=str(row["memory_type"]),
                content=str(row["normalized_content"]),
                summary=str(row["normalized_content"])[:240],
                source_event_ids=(candidate_source.id, source_event_id),
                files=candidate_source.files,
                branch_id=candidate_source.branch_id,
                metadata={
                    "origin": "explicit_marker",
                    "authority_level": "confirmed",
                    "origin_evidence_type": "host_logical_user",
                    "candidate_id": candidate_id,
                },
            )
            memory_id = record.id
        self._link_confirmed_candidate(candidate_id, memory_id, source_event_id)
        return memory_id

    def _link_confirmed_candidate(
        self, candidate_id: str, memory_id: str, source_event_id: str
    ) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO candidate_memory_links"
                "(id, candidate_id, memory_id, relation, source_event_id, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    f"cml_{uuid.uuid4().hex}", candidate_id, memory_id,
                    "confirmed_as", source_event_id, now(),
                ),
            )
