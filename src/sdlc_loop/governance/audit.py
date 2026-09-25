"""Append-only, tamper-evident audit trail.

Every persona call, evaluator verdict, retry, escalation and human decision is
one row. Two layers of protection:

* SQLite triggers reject ``UPDATE`` and ``DELETE`` on the table (append-only).
* Each row stores ``prev_hash`` and ``row_hash = sha256(prev_hash + row)``, so
  any out-of-band edit breaks the chain and :meth:`AuditLog.verify_chain`
  reports the first bad row.

The trail doubles as the evaluation dataset: every metric in docs/evaluation.md
is computed from these rows (see ``evals/metrics.py``).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field

from sdlc_loop.governance.hashing import canonical_json, sha256_hex
from sdlc_loop.governance.pii import Redactor, redact_structure
from sdlc_loop.schemas.common import StrictModel


class AuditEventType(StrEnum):
    REQUIREMENT_REJECTED = "requirement_rejected"
    REQUIREMENT_VALIDATED = "requirement_validated"
    WORKFLOW_PLANNED = "workflow_planned"
    RUN_STARTED = "run_started"
    PII_REDACTED = "pii_redacted"
    PERSONA_CALL = "persona_call"
    PARSE_FAILURE = "parse_failure"
    EVALUATION = "evaluation"
    ESCALATION = "escalation"
    HUMAN_DECISION = "human_decision"
    CODE_VERIFICATION = "code_verification"
    RUN_COMPLETED = "run_completed"
    RUN_ABORTED = "run_aborted"
    RUN_FAILED = "run_failed"


class AuditEvent(StrictModel):
    run_id: str
    event_type: AuditEventType
    persona: str | None = None
    handoff: str | None = None
    attempt: int | None = None
    model: str | None = None
    input_hash: str | None = None
    verdict: str | None = None
    actor: str = "system"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class RunIndexEntry(StrictModel):
    run_id: str
    created: str
    title: str
    rejected: bool


class StoredAuditEvent(AuditEvent):
    id: int
    ts: str
    prev_hash: str
    row_hash: str


_GENESIS = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    run_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    persona TEXT, handoff TEXT, attempt INTEGER, model TEXT,
    input_hash TEXT, verdict TEXT, actor TEXT NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL, cache_read_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL, latency_ms REAL,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    row_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_run ON audit_log(run_id);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

_COLUMNS = (
    "ts, run_id, event_type, persona, handoff, attempt, model, input_hash, verdict, actor, "
    "input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost_usd, latency_ms, "
    "payload, prev_hash, row_hash"
)


_INSERT_SQL = f"INSERT INTO audit_log ({_COLUMNS}) VALUES ({', '.join('?' * 19)})"  # noqa: S608


def _row_hash(prev_hash: str, ts: str, event: AuditEvent) -> str:
    return sha256_hex(prev_hash + ts + canonical_json(event.model_dump(mode="json")))


class AuditLog:
    def __init__(self, path: Path | str, redactor: Redactor) -> None:
        self._redactor = redactor
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def append(self, event: AuditEvent) -> StoredAuditEvent:
        safe = event.model_copy(update={"payload": redact_structure(event.payload, self._redactor)})
        ts = datetime.now(UTC).isoformat()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev = row["row_hash"] if row else _GENESIS
            digest = _row_hash(prev, ts, safe)
            cur = self._conn.execute(
                _INSERT_SQL,
                (
                    ts, safe.run_id, safe.event_type.value, safe.persona, safe.handoff,
                    safe.attempt, safe.model, safe.input_hash, safe.verdict, safe.actor,
                    safe.input_tokens, safe.output_tokens, safe.cache_write_tokens,
                    safe.cache_read_tokens, safe.cost_usd, safe.latency_ms,
                    json.dumps(safe.payload, default=str), prev, digest,
                ),
            )  # fmt: skip
            row_id = cur.lastrowid or 0
        return StoredAuditEvent(
            **safe.model_dump(), id=row_id, ts=ts, prev_hash=prev, row_hash=digest
        )

    def events(self, run_id: str | None = None) -> list[StoredAuditEvent]:
        sql = "SELECT * FROM audit_log"
        params: tuple[str, ...] = ()
        if run_id is not None:
            sql += " WHERE run_id = ?"
            params = (run_id,)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY id", params).fetchall()
        return [self._to_event(r) for r in rows]

    def last_event_id(self, run_id: str) -> int:
        """Id of the run's newest row (0 if none): a cheap change marker for pollers."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(id) AS last FROM audit_log WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row["last"] or 0)

    def recent_runs(self, limit: int) -> list[RunIndexEntry]:
        """Newest runs first, with the orchestrator's title when there is one."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT r.run_id, r.first_ts, t.payload AS decision, t.event_type AS decision_type
                FROM (SELECT run_id, MIN(id) AS first_id, MIN(ts) AS first_ts
                      FROM audit_log GROUP BY run_id ORDER BY first_id DESC LIMIT ?) AS r
                LEFT JOIN audit_log AS t
                  ON t.run_id = r.run_id
                 AND t.event_type IN ('requirement_validated', 'requirement_rejected')
                ORDER BY r.first_id DESC
                """,
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            decision = json.loads(row["decision"]) if row["decision"] else {}
            assessment = decision.get("assessment") or {}
            out.append(
                RunIndexEntry(
                    run_id=row["run_id"],
                    created=row["first_ts"],
                    title=assessment.get("title") or str(decision.get("requirement", ""))[:60],
                    rejected=row["decision_type"] == AuditEventType.REQUIREMENT_REJECTED.value,
                )
            )
        return out

    def verify_chain(self) -> tuple[bool, int | None]:
        """Return ``(ok, first_bad_row_id)``."""
        prev = _GENESIS
        for event in self.events():
            base = AuditEvent(**event.model_dump(exclude={"id", "ts", "prev_hash", "row_hash"}))
            if event.prev_hash != prev or _row_hash(prev, event.ts, base) != event.row_hash:
                return False, event.id
            prev = event.row_hash
        return True, None

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _to_event(row: sqlite3.Row) -> StoredAuditEvent:
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        return StoredAuditEvent(**data)
