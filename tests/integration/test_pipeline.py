"""Full LangGraph runs against the recorded Scenario 1 script (offline, deterministic).

Each test perturbs the script to drive one path through the reliability engine:
happy path, retry, exhaustion, critical fast-track, human accept/retry/abort,
tiered judge confirmation, deterministic pre-gate, parse repair, fail-closed judge.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from evals.metrics import compute_metrics
from sdlc_loop.config import ContextPolicy, CriticalPolicy, Settings
from sdlc_loop.governance.audit import AuditEventType
from sdlc_loop.llm.fake_client import ScriptedLLMClient
from sdlc_loop.schemas import Handoff, HumanDecision, RunStatus, Verdict
from sdlc_loop.services import RunService, build_container
from tests.conftest import ServiceFactory, judge

SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5-20251001"
CRITICAL = judge(3, 2, 1, 3, 3, feedback="assumes a REST API the mainframe does not have")


def _calls(llm: ScriptedLLMClient, purpose: str) -> list[Any]:
    return [c for c in llm.calls if c.tags.purpose == purpose]


def _evals(snap: Any, handoff: Handoff) -> list[Any]:
    return [e for e in snap.evaluations if e.handoff is handoff]


def test_happy_path_with_one_architect_retry(
    make_service: ServiceFactory, script: dict[str, list[Any]], scenario1_request: str
) -> None:
    service, llm = make_service(script)
    snap = service.start(scenario1_request, scenario_id="1")

    assert snap.status is RunStatus.COMPLETE
    assert set(snap.artifacts) == {
        "problem_brief",
        "requirements",
        "design",
        "implementation",
        "test_plan",
    }
    arch = _evals(snap, Handoff.ARCHITECT_TO_DEV)
    assert [e.verdict for e in arch] == [Verdict.RETRY, Verdict.PASS]
    assert arch[0].critical_flag
    # the retry saw the evaluator's feedback and its own previous attempt
    retry_prompt = _calls(llm, "persona:architect")[1].user_content
    assert "<evaluator_feedback_1>" in retry_prompt
    assert "<previous_attempt>" in retry_prompt
    # QA's execution report is attached by code, not claimed by the model
    assert snap.artifacts["test_plan"]["execution"]["files_checked"] == 4
    # routing: PM/BA/QA/judge on Tier 1, Architect/Dev on Tier 2
    assert {c.model_id for c in _calls(llm, "persona:pm")} == {HAIKU}
    assert {c.model_id for c in _calls(llm, "persona:architect")} == {SONNET}
    assert {c.model_id for c in _calls(llm, "persona:dev")} == {SONNET}


def test_audit_trail_is_complete_and_intact(
    make_service: ServiceFactory, script: dict[str, list[Any]], scenario1_request: str
) -> None:
    service, _ = make_service(script)
    snap = service.start(scenario1_request, scenario_id="1")
    events = service._c.audit.events(snap.run_id)
    types = [e.event_type for e in events]
    assert types[0] is AuditEventType.RUN_STARTED
    assert types[-1] is AuditEventType.RUN_COMPLETED
    assert types.count(AuditEventType.EVALUATION) == 5  # 4 gates + 1 architect retry
    assert AuditEventType.CODE_VERIFICATION in types
    calls = [e for e in events if e.event_type is AuditEventType.PERSONA_CALL]
    assert all(e.input_hash and e.model and e.cost_usd > 0 for e in calls)
    assert service._c.audit.verify_chain() == (True, None)


def test_persistent_critical_defect_escalates_then_human_accepts(
    make_service: ServiceFactory, script: dict[str, list[Any]], scenario1_request: str
) -> None:
    script["persona:architect"] = [script["persona:architect"][0]]  # always flawed
    script["judge:architect_to_dev:tier1"] = [CRITICAL]
    script["judge:architect_to_dev:tier2"] = [CRITICAL]
    service, llm = make_service(script)

    snap = service.start(scenario1_request, scenario_id="1")
    assert snap.status is RunStatus.BLOCKED_FOR_HUMAN
    review = snap.pending_review
    assert review is not None and review.handoff is Handoff.ARCHITECT_TO_DEV
    assert review.critical_flag and review.attempts_so_far == 2
    arch = _evals(snap, Handoff.ARCHITECT_TO_DEV)
    # attempt 1: critical -> fast-track retry
    # attempt 2: still critical -> confirmed by Tier 2 -> escalate
    assert [e.verdict for e in arch] == [Verdict.RETRY, Verdict.ESCALATE]
    assert arch[1].judge == "tier2" and arch[1].judge_escalated
    assert "persona:dev" not in {c.tags.purpose for c in llm.calls}  # blocked means blocked

    snap = service.resume(snap.run_id, HumanDecision(action="accept_as_is", reviewer="alice"))
    assert snap.status is RunStatus.COMPLETE
    assert snap.human_decisions[0].reviewer == "alice"
    events = service._c.audit.events(snap.run_id)
    decision = next(e for e in events if e.event_type is AuditEventType.HUMAN_DECISION)
    assert decision.actor == "alice" and decision.verdict == "accept_as_is"


def test_strict_policy_escalates_on_first_critical(
    make_service: ServiceFactory, script: dict[str, list[Any]], scenario1_request: str
) -> None:
    script["judge:architect_to_dev:tier2"] = [CRITICAL]
    script["judge:architect_to_dev:tier1"] = [CRITICAL]
    service, _ = make_service(script, critical_policy=CriticalPolicy.STRICT)
    snap = service.start(scenario1_request)
    assert snap.status is RunStatus.BLOCKED_FOR_HUMAN
    assert [e.verdict for e in _evals(snap, Handoff.ARCHITECT_TO_DEV)] == [Verdict.ESCALATE]


def test_human_retry_passes_guidance_and_forces_tier2(
    make_service: ServiceFactory, script: dict[str, list[Any]], scenario1_request: str
) -> None:
    flawed, good = script["persona:architect"]
    script["persona:architect"] = [flawed, flawed, good]
    script["judge:architect_to_dev:tier1"] = [CRITICAL, CRITICAL, judge(5, 4, 5, 4, 4)]
    script["judge:architect_to_dev:tier2"] = [CRITICAL]
    service, llm = make_service(script)
    snap = service.start(scenario1_request)
    assert snap.status is RunStatus.BLOCKED_FOR_HUMAN

    guidance = "Consume the nightly settlement extract; return decisions as a hold file."
    snap = service.resume(
        snap.run_id, HumanDecision(action="retry", guidance=guidance, reviewer="bob")
    )
    assert snap.status is RunStatus.COMPLETE
    third = _calls(llm, "persona:architect")[2]
    assert guidance in third.user_content
    assert third.model_id == SONNET
    assert _evals(snap, Handoff.ARCHITECT_TO_DEV)[-1].verdict is Verdict.PASS


def test_human_abort_stops_the_run(
    make_service: ServiceFactory, script: dict[str, list[Any]], scenario1_request: str
) -> None:
    script["judge:pm_to_ba:tier1"] = [CRITICAL]
    script["judge:pm_to_ba:tier2"] = [CRITICAL]  # confirmation agrees, so a human is paged
    service, llm = make_service(script, critical_policy=CriticalPolicy.STRICT)
    snap = service.start(scenario1_request)
    snap = service.resume(snap.run_id, HumanDecision(action="abort", reviewer="carol"))
    assert snap.status is RunStatus.ABORTED
    assert "persona:ba" not in {c.tags.purpose for c in llm.calls}
    types = [e.event_type for e in service._c.audit.events(snap.run_id)]
    assert AuditEventType.RUN_ABORTED in types and AuditEventType.RUN_COMPLETED not in types


def test_non_critical_failures_exhaust_retries_with_tier_escalation(
    make_service: ServiceFactory, script: dict[str, list[Any]], scenario1_request: str
) -> None:
    weak = judge(4, 3, 3, 3, 3, feedback="acceptance criteria are not testable")  # 3.2, borderline
    script["judge:ba_to_architect:tier1"] = [weak]
    script["judge:ba_to_architect:tier2"] = [weak]
    service, llm = make_service(script)
    snap = service.start(scenario1_request)

    ba = _evals(snap, Handoff.BA_TO_ARCHITECT)
    assert [e.verdict for e in ba] == [Verdict.RETRY, Verdict.RETRY, Verdict.ESCALATE]
    assert all(
        e.judge_escalated and e.tier1_overall_score == 3.2 for e in ba
    )  # borderline -> Tier 2
    assert [c.model_id for c in _calls(llm, "persona:ba")] == [HAIKU, HAIKU, SONNET]
    final_prompt = _calls(llm, "persona:ba")[2].user_content
    assert "<evaluator_feedback_1>" in final_prompt and "<evaluator_feedback_2>" in final_prompt
    assert snap.status is RunStatus.BLOCKED_FOR_HUMAN


def test_tier2_judge_can_overturn_borderline_tier1(
    make_service: ServiceFactory, script: dict[str, list[Any]], scenario1_request: str
) -> None:
    script["judge:pm_to_ba:tier1"] = [judge(4, 4, 3, 3, 3)]  # 3.4 borderline
    script["judge:pm_to_ba:tier2"] = [judge(5, 4, 5, 4, 4)]
    service, _ = make_service(script)
    snap = service.start(scenario1_request)
    pm = _evals(snap, Handoff.PM_TO_BA)
    assert len(pm) == 1
    assert pm[0].verdict is Verdict.PASS
    assert pm[0].judge == "tier2" and pm[0].tier1_overall_score == 3.4


def test_deterministic_pregate_rejects_without_a_judge_call(
    make_service: ServiceFactory, script: dict[str, list[Any]], scenario1_request: str
) -> None:
    good = script["persona:dev"][0]
    broken = {
        **good,
        "files": [{**good["files"][1], "content": "def broken(:\n"}, *good["files"][2:]],
    }
    script["persona:dev"] = [broken, good]
    service, llm = make_service(script)
    snap = service.start(scenario1_request)
    dev = _evals(snap, Handoff.DEV_TO_QA)
    assert [(e.judge, e.verdict) for e in dev] == [
        ("deterministic", Verdict.RETRY),
        ("tier1", Verdict.PASS),
    ]
    assert "does not parse" in dev[0].feedback
    assert len(_calls(llm, "judge:dev_to_qa:tier1")) == 1  # only the good attempt was judged
    assert snap.status is RunStatus.COMPLETE


def test_schema_parse_failure_is_repaired_and_audited(
    make_service: ServiceFactory, script: dict[str, list[Any]], scenario1_request: str
) -> None:
    script["persona:pm"] = ['{"objective": "truncated', script["persona:pm"][0]]
    service, llm = make_service(script)
    snap = service.start(scenario1_request)
    assert snap.status is RunStatus.COMPLETE
    assert "<schema_validation_error>" in _calls(llm, "persona:pm")[1].user_content
    types = [e.event_type for e in service._c.audit.events(snap.run_id)]
    assert types.count(AuditEventType.PARSE_FAILURE) == 1


def test_unusable_judge_output_fails_closed(
    make_service: ServiceFactory, script: dict[str, list[Any]], scenario1_request: str
) -> None:
    script["judge:pm_to_ba:tier1"] = ["not json at all"]
    service, _ = make_service(script)
    snap = service.start(scenario1_request)
    assert snap.status is RunStatus.BLOCKED_FOR_HUMAN
    record = _evals(snap, Handoff.PM_TO_BA)[0]
    assert record.verdict is Verdict.ESCALATE and "failing closed" in record.feedback


def test_run_survives_process_restart(
    make_settings: Callable[..., Settings], script: dict[str, list[Any]], scenario1_request: str
) -> None:
    """State lives in the SQLite checkpointer, not in memory: a new process can resume."""
    script["judge:architect_to_dev:tier1"] = [CRITICAL]
    script["judge:architect_to_dev:tier2"] = [CRITICAL]
    first = build_container(make_settings(), llm=ScriptedLLMClient(script))
    run_id = RunService(first).start(scenario1_request).run_id

    first.close()

    second = build_container(make_settings(), llm=ScriptedLLMClient(script))  # "new process"
    service = RunService(second)
    assert service.get(run_id).status is RunStatus.BLOCKED_FOR_HUMAN
    assert service.resume(run_id, HumanDecision(action="accept_as_is")).status is RunStatus.COMPLETE
    second.close()


def test_pii_is_redacted_before_any_model_call(
    make_service: ServiceFactory, script: dict[str, list[Any]]
) -> None:
    service, llm = make_service(script)
    request = "Refund flow please. Customer jane.doe@example.com paid with 4111 1111 1111 1111."
    snap = service.start(request)
    assert all("jane.doe" not in c.user_content and "4111" not in c.user_content for c in llm.calls)
    assert "<EMAIL_1>" in _calls(llm, "persona:pm")[0].user_content
    redaction = next(
        e
        for e in service._c.audit.events(snap.run_id)
        if e.event_type is AuditEventType.PII_REDACTED
    )
    assert redaction.payload["entity_counts"] == {"EMAIL": 1, "CREDIT_CARD": 1}


def test_minimal_context_is_smaller_than_full_history(
    make_service: ServiceFactory, script: dict[str, list[Any]], scenario1_request: str
) -> None:
    minimal, llm_min = make_service(script)
    minimal.start(scenario1_request)
    full, llm_full = make_service(script, context_policy=ContextPolicy.FULL_HISTORY)
    full.start(scenario1_request)
    qa_min = _calls(llm_min, "persona:qa")[0].user_content
    qa_full = _calls(llm_full, "persona:qa")[0].user_content
    assert "<problem_brief>" not in qa_min and "<problem_brief>" in qa_full
    assert "<evaluation_history>" in qa_full
    assert len(qa_full) > len(qa_min)


def test_metrics_from_audit_log(
    make_service: ServiceFactory, script: dict[str, list[Any]], scenario1_request: str
) -> None:
    service, _ = make_service(script)
    for i in range(2):
        service.start(scenario1_request, scenario_id="1", run_id=f"m-{i}")
    m = compute_metrics(service._c.audit.events())
    assert m.runs == 2 and m.completion_rate_without_human == 1.0
    arch = next(h for h in m.handoffs if h.handoff == "Architect→Dev")
    assert (arch.attempt1_pass_rate, arch.retry_rate, arch.mean_attempts) == (0.0, 1.0, 2.0)
    pm = next(h for h in m.handoffs if h.handoff == "PM→BA")
    assert pm.attempt1_pass_rate == 1.0
    c = m.cost
    assert c.all_tier2_usd > c.no_cache_sync_usd >= c.sync_usd >= c.actual_usd > 0
    assert c.naive_context_tokens > c.minimal_context_tokens
    assert c.parse_failure_rate == 0.0
    assert m.qa_uncovered_criteria_per_run == 0.0
