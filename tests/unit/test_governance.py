from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from sdlc_loop.governance.audit import AuditEvent, AuditEventType, AuditLog
from sdlc_loop.governance.pii import RegexRedactor, redact_structure
from sdlc_loop.scenarios import load_scenarios
from tests.conftest import SCENARIOS


def test_redacts_structured_identifiers() -> None:
    text = (
        "Contact jane.doe@example.com, card 4111 1111 1111 1111, SSN 123-45-6789, "
        "phone +1 415 555 0132. Email jane.doe@example.com again."
    )
    result = RegexRedactor().redact(text)
    assert "jane.doe" not in result.text
    assert "4111" not in result.text
    assert "123-45-6789" not in result.text
    assert "555 0132" not in result.text
    assert result.counts["EMAIL"] == 1  # same value -> same placeholder
    assert result.text.count("<EMAIL_1>") == 2


def test_card_requires_luhn() -> None:
    # 16 digits failing the Luhn check are not classified as a card (another, more
    # conservative pattern may still mask them — over-redaction is the safe direction)
    assert "CREDIT_CARD" not in RegexRedactor().redact("ref 4111 1111 1111 1112").counts
    assert RegexRedactor().redact("ref 4111 1111 1111 1111").counts == {"CREDIT_CARD": 1}


def test_identifiers_are_not_mistaken_for_phone_numbers() -> None:
    # Regression: the audit log's second-pass redaction used to turn the model id
    # claude-haiku-4-5-20251001 into claude-haiku-4-<PHONE_1> in the browser view.
    for text in ("claude-haiku-4-5-20251001", "run-39d052021b93", "US-1-AC2", "2026-12-21"):
        assert RegexRedactor().redact(text).text == text
    assert RegexRedactor().redact("call 415-555-0132 now").counts == {"PHONE": 1}


def test_golden_scenarios_have_no_false_positives() -> None:
    for scenario in load_scenarios(SCENARIOS):
        result = RegexRedactor().redact(scenario.request)
        assert not result.redacted, (scenario.id, result.counts)


def test_redact_structure_is_recursive() -> None:
    out = redact_structure({"a": ["mail me: x@y.io", {"b": "ok"}], "n": 3}, RegexRedactor())
    assert out == {"a": ["mail me: <EMAIL_1>", {"b": "ok"}], "n": 3}


@pytest.fixture
def audit(tmp_path: Path) -> Iterator[AuditLog]:
    log = AuditLog(tmp_path / "audit.db", RegexRedactor())
    yield log
    log.close()


def test_audit_append_read_and_redaction(audit: AuditLog) -> None:
    audit.append(AuditEvent(run_id="r1", event_type=AuditEventType.RUN_STARTED))
    stored = audit.append(
        AuditEvent(
            run_id="r1",
            event_type=AuditEventType.PERSONA_CALL,
            persona="pm",
            input_tokens=10,
            payload={"output": {"note": "customer bob@corp.com"}},
        )
    )
    assert stored.payload["output"]["note"] == "customer <EMAIL_1>"
    events = audit.events("r1")
    assert [e.event_type for e in events] == [
        AuditEventType.RUN_STARTED,
        AuditEventType.PERSONA_CALL,
    ]
    assert [r.run_id for r in audit.recent_runs(10)] == ["r1"]
    assert audit.last_event_id("r1") == events[-1].id
    assert audit.last_event_id("nope") == 0
    assert audit.verify_chain() == (True, None)


def test_audit_is_append_only(audit: AuditLog, tmp_path: Path) -> None:
    audit.append(AuditEvent(run_id="r1", event_type=AuditEventType.RUN_STARTED))
    with closing(sqlite3.connect(tmp_path / "audit.db")) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE audit_log SET run_id = 'x'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM audit_log")


def test_audit_tampering_is_detected(audit: AuditLog, tmp_path: Path) -> None:
    for i in range(3):
        audit.append(
            AuditEvent(
                run_id="r1", event_type=AuditEventType.EVALUATION, verdict="retry", attempt=i
            )
        )
    with closing(sqlite3.connect(tmp_path / "audit.db")) as conn:
        # an attacker with file access drops the trigger and rewrites a verdict
        conn.execute("DROP TRIGGER audit_log_no_update")
        conn.execute("UPDATE audit_log SET verdict = 'pass' WHERE id = 2")
        conn.commit()
    assert audit.verify_chain() == (False, 2)
