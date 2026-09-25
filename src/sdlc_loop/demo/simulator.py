"""Deterministic stand-in for Claude when no API key is configured.

It lets demo mode run *any* requirement through the real orchestrator, graph,
evaluator gates, retry logic, code verifier and audit log. It is not a language
model and never claims to be one: every response reports the model id
``local-simulator`` (priced at $0) and the UI labels such runs "Simulated".

* **Agents** fill schema-valid templates from the requirement text. The PM's
  first attempt is a deliberately thin draft (two goals, unmeasurable metrics),
  so every simulated run exercises the evaluator's RETRY path; the retry, which
  receives the evaluator's feedback, produces the complete brief.
* **The judge** is rule-based: it scores the artifact actually under review
  against the rubric's countable expectations and explains every deduction.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

from sdlc_loop.llm.token_meter import estimate_tokens
from sdlc_loop.llm.types import LLMRequest, LLMResponse, TokenUsage
from sdlc_loop.schemas.common import Persona

SIMULATOR_MODEL = "local-simulator"
_LEADING_VERBS = re.compile(
    r"^(please\s+)?(build|create|develop|implement|design|add|make|write|deliver|set\s+up)\s+"
    r"(an?\s+|the\s+)?",
    re.IGNORECASE,
)
_TAG = r"<{tag}>\n(.*?)\n</{tag}>"


def subject_of(requirement: str) -> str:
    """'Build a Leave Management System.' -> 'Leave Management System'."""
    text = re.sub(r"\s+", " ", requirement).strip().rstrip(".!")
    text = _LEADING_VERBS.sub("", text)
    words = text.split(" ")
    subject = " ".join(words[:8])
    return subject[:1].upper() + subject[1:] if subject else "the requested system"


def _section(content: str, tag: str) -> str | None:
    match = re.search(_TAG.format(tag=tag), content, re.DOTALL)
    return match.group(1) if match else None


def _json_section(content: str, tag: str) -> Any:
    raw = _section(content, tag)
    return json.loads(raw) if raw else None


def _brief(subject: str, complete: bool) -> dict[str, Any]:
    constraints = [
        "The requirement names no existing systems; integration targets are unconfirmed "
        "and must not be assumed.",
        f"Business rules for {subject} are not specified and must be confirmed with the "
        "business owner.",
        "Records may contain personal data and fall under the organisation's "
        "data-protection obligations.",
    ]
    if not complete:
        return {
            "objective": f"Deliver {subject}.",
            "business_goals": [f"Users can use {subject}.", "The process becomes faster."],
            "success_metrics": ["Users are satisfied.", "Processing is more efficient."],
            "constraints": constraints,
            "out_of_scope": [],
            "stakeholders": ["Users"],
        }
    return {
        "objective": f"Provide a self-service {subject} that replaces manual handling with "
        "a tracked, auditable workflow.",
        "business_goals": [
            f"Users can submit and track {subject} requests without manual follow-up.",
            "Approvers can review and decide requests with full context.",
            "Business rules are applied consistently to every request.",
            "Every action is recorded for audit.",
        ],
        "success_metrics": [
            ">= 80% of requests submitted through the system within 3 months of launch.",
            "Median decision turnaround <= 2 business days.",
            "100% of decisions traceable to an actor and timestamp in the audit log.",
        ],
        "constraints": constraints,
        "out_of_scope": [
            "Integration with external systems until they are confirmed.",
            "Reporting beyond the standard audit export in phase 1.",
        ],
        "stakeholders": [
            "End users",
            "Approvers",
            "Business owner",
            "IT operations",
            "Security and compliance",
        ],
    }


def _ac(story: int, n: int, text: str) -> dict[str, str]:
    return {"id": f"US-{story}-AC{n}", "description": text}


def _requirements(subject: str) -> dict[str, Any]:
    stories = [
        (
            "user",
            f"to submit a {subject} request with the required details",
            "the request enters a tracked workflow",
            [
                "A complete request is saved with status SUBMITTED and a unique id.",
                "A request missing a required field is rejected with the missing field named.",
            ],
        ),
        (
            "approver",
            "to approve or reject submitted requests",
            "decisions are made promptly",
            [
                "Approving a SUBMITTED request sets its status to APPROVED.",
                "Rejecting a request requires a non-empty reason, which is stored with it.",
                "A request that is not SUBMITTED cannot be approved or rejected.",
            ],
        ),
        (
            "user",
            "to see my requests and their current status",
            "I know what is pending",
            [
                "The list shows only the current user's requests, newest first.",
                "Each entry shows its status and last-updated time.",
            ],
        ),
        (
            "compliance officer",
            "an audit trail of every action",
            "decisions can be evidenced",
            [
                "Every create, approve and reject action is recorded with actor, time and "
                "old and new status.",
                "Audit entries cannot be edited or deleted through the application.",
            ],
        ),
    ]
    return {
        "user_stories": [
            {
                "id": f"US-{i}",
                "as_a": role,
                "i_want": want,
                "so_that": why,
                "acceptance_criteria": [_ac(i, n, t) for n, t in enumerate(acs, start=1)],
            }
            for i, (role, want, why, acs) in enumerate(stories, start=1)
        ],
        "non_functional_requirements": [
            "Security: role-based access for users, approvers and compliance officers.",
            "Privacy: personal data is visible only to the requester, approvers and compliance.",
            "Performance: list and detail views respond in < 500 ms p95.",
            "Integration: no external system is assumed; integrations sit behind adapters.",
        ],
        "open_questions": [
            "Which identity provider and user directory should be used?",
            f"What are the exact business rules and approval levels for {subject}?",
            "What are the data-retention requirements for requests and audit entries?",
        ],
        "traceability": [
            {"story_id": "US-1", "business_goal_indices": [0]},
            {"story_id": "US-2", "business_goal_indices": [1, 2]},
            {"story_id": "US-3", "business_goal_indices": [0]},
            {"story_id": "US-4", "business_goal_indices": [3]},
        ],
    }


def _design(subject: str) -> dict[str, Any]:
    return {
        "components": [
            {"name": "Web UI", "responsibility": "Request forms, approval queue, status views."},
            {
                "name": "Request Service",
                "responsibility": f"Validates and stores {subject} "
                "requests; enforces the status workflow.",
            },
            {"name": "Rules Module", "responsibility": "Data-driven validation and approval."},
            {
                "name": "Integration Adapters",
                "responsibility": "Interfaces for identity and any "
                "downstream system, local implementations until targets are confirmed.",
            },
            {"name": "Audit Log", "responsibility": "Append-only record of every action."},
        ],
        "integration_approach": "No external system is confirmed, so identity and any "
        "downstream integration sit behind adapter interfaces with local first implementations "
        "(local accounts, file export). When the business confirms the real systems only the "
        "adapters change; the Request Service and Rules Module are unaffected.",
        "data_flow": "User submits a request in the Web UI -> Request Service validates it with "
        "the Rules Module and stores it as SUBMITTED -> Audit Log records the action -> approver "
        "decides in the approval queue -> status updated and audited -> user sees the new status.",
        "risks": [
            {
                "description": "Business rules are more complex than assumed.",
                "mitigation": "Rules are data-driven; validate against worked examples from the "
                "business owner before launch.",
            },
            {
                "description": "Integration targets change after build starts.",
                "mitigation": "All integrations are behind adapters with contract tests, so a "
                "change is contained to one adapter.",
            },
        ],
        "adrs": [
            {
                "title": "Adapters for unconfirmed integrations",
                "decision": "Put identity and downstream systems behind adapters.",
                "alternatives_considered": ["Build against a presumed system API"],
                "rationale": "The requirement names no systems; adapters avoid inventing them.",
            },
            {
                "title": "Modular monolith",
                "decision": "One deployable with clear module boundaries.",
                "alternatives_considered": ["Microservices per component"],
                "rationale": "Small scope; boundaries allow later extraction.",
            },
        ],
    }


_SERVICE_PY = '''"""Request Service with status workflow and audit trail (in-memory slice)."""
from datetime import datetime, timezone
from itertools import count

REQUIRED_FIELDS = ("title", "requester")
SUBMITTED, APPROVED, REJECTED = "SUBMITTED", "APPROVED", "REJECTED"


class RequestError(ValueError):
    pass


class RequestService:
    def __init__(self):
        self._ids = count(1)
        self.requests = {}
        self.audit = []

    def submit(self, data):
        missing = [f for f in REQUIRED_FIELDS if not str(data.get(f, "")).strip()]
        if missing:
            raise RequestError(f"missing required field: {missing[0]}")
        request = {**data, "id": next(self._ids), "status": SUBMITTED, "reason": ""}
        self.requests[request["id"]] = request
        self._record("create", data["requester"], request, None)
        return request

    def approve(self, approver, request_id):
        return self._decide(approver, request_id, APPROVED, "")

    def reject(self, approver, request_id, reason):
        if not reason or not reason.strip():
            raise RequestError("a rejection reason is required")
        return self._decide(approver, request_id, REJECTED, reason)

    def _decide(self, approver, request_id, status, reason):
        request = self.requests[request_id]
        if request["status"] != SUBMITTED:
            raise RequestError(f"request {request_id} is already {request['status']}")
        previous = request["status"]
        request["status"], request["reason"] = status, reason
        self._record(status.lower(), approver, request, previous)
        return request

    def _record(self, action, actor, request, previous):
        self.audit.append({"action": action, "actor": actor, "request_id": request["id"],
                           "previous_status": previous, "new_status": request["status"],
                           "at": datetime.now(timezone.utc).isoformat()})
'''

_TEST_PY = """import pytest

from app.service import RequestError, RequestService


def test_valid_request_is_submitted_with_unique_id():
    service = RequestService()
    first = service.submit({"title": "A", "requester": "u1"})
    second = service.submit({"title": "B", "requester": "u1"})
    assert first["status"] == "SUBMITTED"
    assert first["id"] != second["id"]


def test_missing_required_field_is_named():
    with pytest.raises(RequestError, match="title"):
        RequestService().submit({"requester": "u1"})


def test_approve_reject_rules_and_audit():
    service = RequestService()
    a = service.submit({"title": "A", "requester": "u1"})
    b = service.submit({"title": "B", "requester": "u1"})
    service.approve("mgr", a["id"])
    with pytest.raises(RequestError, match="reason"):
        service.reject("mgr", b["id"], " ")
    service.reject("mgr", b["id"], "duplicate")
    with pytest.raises(RequestError, match="already"):
        service.approve("mgr", b["id"])
    assert [e["action"] for e in service.audit] == ["create", "create", "approved", "rejected"]
"""


def _implementation(subject: str) -> dict[str, Any]:
    return {
        "files": [
            {"path": "app/__init__.py", "language": "python", "content": ""},
            {"path": "app/service.py", "language": "python", "content": _SERVICE_PY},
            {"path": "test_service.py", "language": "python", "content": _TEST_PY},
        ],
        "implemented_story_ids": ["US-1", "US-2"],
        "stubbed_stories": [
            {
                "story_id": "US-3",
                "plan": "List endpoint over RequestService filtered by "
                "requester, sorted by last update.",
            },
            {
                "story_id": "US-4",
                "plan": "Move the in-memory audit list to an append-only "
                "table without update/delete grants.",
            },
        ],
        "deviations": ["Storage is in-memory in this slice."],
        "summary": f"Implemented the Request Service for {subject}: submission with required-"
        "field validation (US-1) and the approve/reject workflow with mandatory rejection "
        "reasons and an audit entry per action (US-2). US-3 and US-4 are stubbed with plans.",
    }


def _test_plan() -> dict[str, Any]:
    cases = [
        ("US-1", ["US-1-AC1"], "unit", "Submit two valid requests", "SUBMITTED; ids differ."),
        ("US-1", ["US-1-AC2"], "unit", "Submit without a title", "Rejected naming 'title'."),
        (
            "US-2",
            ["US-2-AC1", "US-2-AC2", "US-2-AC3"],
            "unit",
            "Approve one request; reject another with a blank then a real reason; re-approve it",
            "Blank reason refused; decided request cannot be decided again.",
        ),
        (
            "US-2",
            ["US-1-AC1", "US-2-AC1"],
            "e2e",
            "Submit in the UI, approve in the queue",
            "Requester sees APPROVED.",
        ),
        (
            "US-4",
            ["US-4-AC1"],
            "unit",
            "Inspect audit entries after the workflow",
            "create, create, approved, rejected recorded with actor and statuses.",
        ),
    ]
    return {
        "test_cases": [
            {
                "id": f"TC-{i}",
                "story_id": s,
                "acceptance_criterion_ids": acs,
                "type": t,
                "steps": [step],
                "expected_result": exp,
            }
            for i, (s, acs, t, step, exp) in enumerate(cases, start=1)
        ],
        "coverage_summary": "US-1 and US-2 are covered by the unit tests shipped with the "
        "implementation; US-4-AC1 is checked through the in-memory audit list.",
        "gaps": ["No UI yet, so the e2e case cannot run.", "Persistence is in-memory."],
        "untestable_criteria": ["US-3-AC1", "US-3-AC2", "US-4-AC2"],
    }


Scores = dict[str, int]


def _judge_brief(a: dict[str, Any]) -> tuple[Scores, list[str]]:
    issues = []
    goals, metrics = a.get("business_goals", []), a.get("success_metrics", [])
    if not 3 <= len(goals) <= 5:
        issues.append(f"Expected 3-5 measurable business goals, found {len(goals)}.")
    vague = [m for m in metrics if not re.search(r"\d", m)]
    if vague:
        issues.append(f"Success metrics must be measurable; add a number to: {'; '.join(vague)}")
    if not a.get("out_of_scope"):
        issues.append("State what is out of scope for this phase.")
    scores = {
        "completeness": 5 if len(goals) >= 3 and a.get("out_of_scope") else 3,
        "correctness_feasibility": 4,
        "legacy_constraint_awareness": 5 if a.get("constraints") else 1,
        "clarity_actionability": 3 if vague else 5,
        "traceability": 4,
    }
    return scores, issues


def _judge_requirements(a: dict[str, Any]) -> tuple[Scores, list[str]]:
    stories = a.get("user_stories", [])
    traced = {t["story_id"] for t in a.get("traceability", [])}
    untraced = [s["id"] for s in stories if s["id"] not in traced]
    thin = [s["id"] for s in stories if len(s.get("acceptance_criteria", [])) < 2]
    issues = [f"Stories without traceability: {', '.join(untraced)}."] if untraced else []
    issues += [f"Stories with fewer than 2 acceptance criteria: {', '.join(thin)}."] if thin else []
    scores = {
        "completeness": 5 if 4 <= len(stories) <= 8 and not thin else 3,
        "correctness_feasibility": 4,
        "legacy_constraint_awareness": 5 if a.get("open_questions") else 3,
        "clarity_actionability": 4,
        "traceability": 5 if not untraced else 2,
    }
    return scores, issues


def _judge_design(a: dict[str, Any]) -> tuple[Scores, list[str]]:
    risks = a.get("risks", [])
    unmitigated = [r["description"] for r in risks if len(r.get("mitigation", "")) < 10]
    issues = [f"Risks without mitigation: {'; '.join(unmitigated)}"] if unmitigated else []
    if len(risks) < 2:
        issues.append("List at least two risks.")
    adapter = "adapter" in a.get("integration_approach", "").lower()
    scores = {
        "completeness": 5 if len(risks) >= 2 and not unmitigated else 3,
        "correctness_feasibility": 4,
        "legacy_constraint_awareness": 5 if adapter else 3,
        "clarity_actionability": 4,
        "traceability": 4,
    }
    return scores, issues


def _judge_implementation(a: dict[str, Any]) -> tuple[Scores, list[str]]:
    broken = []
    for f in a.get("files", []):
        if f["path"].endswith(".py"):
            try:
                ast.parse(f["content"])
            except SyntaxError:
                broken.append(f["path"])
    issues = [f"Code does not parse: {', '.join(broken)}."] if broken else []
    scores = {
        "completeness": 5 if a.get("stubbed_stories") is not None else 3,
        "correctness_feasibility": 1 if broken else 4,
        "legacy_constraint_awareness": 5,
        "clarity_actionability": 4,
        "traceability": 5 if a.get("implemented_story_ids") else 2,
    }
    return scores, issues


_JUDGES = {
    "PM→BA": _judge_brief,
    "BA→Architect": _judge_requirements,
    "Architect→Dev": _judge_design,
    "Dev→QA": _judge_implementation,
}


def _judgement(content: str) -> dict[str, Any]:
    handoff = (_section(content, "handoff_under_review") or "").strip()
    artifact = _json_section(content, "artifact_under_review") or {}
    scores, issues = _JUDGES[handoff](artifact)
    if issues:
        feedback = "Rule-based review found: " + " ".join(issues)
    else:
        feedback = f"{handoff}: all rubric checks satisfied (rule-based simulated judge)."
    return {"dimension_scores": scores, "blocking_issues": issues, "feedback": feedback}


class SimulatedLLMClient:
    """Implements the ``LLMClient`` protocol without calling any model."""

    def __init__(self) -> None:
        self._subjects: dict[str, str] = {}

    def complete(self, request: LLMRequest) -> LLMResponse:
        content = request.user_content
        persona = request.tags.persona
        if persona is Persona.EVALUATOR:
            payload = _judgement(content)
        else:
            payload = self._artifact(request.tags.run_id, persona, request.tags.attempt, content)
        text = json.dumps(payload)
        return LLMResponse(
            text=text,
            usage=TokenUsage(
                input_tokens=sum(estimate_tokens(b.text) for b in request.system)
                + estimate_tokens(content),
                output_tokens=estimate_tokens(text),
            ),
            model_id=SIMULATOR_MODEL,
            stop_reason="end_turn",
            latency_ms=0.0,
        )

    def _artifact(self, run_id: str, persona: Persona, attempt: int, content: str) -> Any:
        if persona is Persona.PM:
            request = _section(content, "untrusted_business_request") or ""
            self._subjects[run_id] = subject_of(request)
        subject = self._subjects.get(run_id, "the requested system")
        match persona:
            case Persona.PM:
                return _brief(subject, complete=attempt > 1)
            case Persona.BA:
                return _requirements(subject)
            case Persona.ARCHITECT:
                return _design(subject)
            case Persona.DEV:
                return _implementation(subject)
            case Persona.QA:
                return _test_plan()
            case _:
                raise ValueError(f"simulator has no template for {persona}")
