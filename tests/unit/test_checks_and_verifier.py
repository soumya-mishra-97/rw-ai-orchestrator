from __future__ import annotations

from typing import Any

import pytest

from sdlc_loop.quality import checks
from sdlc_loop.schemas import (
    Implementation,
    ProblemBrief,
    RequirementsPackage,
    TechnicalDesign,
    TestPlan,
)
from sdlc_loop.tools.code_verifier import CodeVerifier


def _artifacts(
    script: dict[str, list[Any]],
) -> tuple[ProblemBrief, RequirementsPackage, TechnicalDesign, Implementation]:
    return (
        ProblemBrief.model_validate(script["persona:pm"][0]),
        RequirementsPackage.model_validate(script["persona:ba"][0]),
        TechnicalDesign.model_validate(script["persona:architect"][1]),
        Implementation.model_validate(script["persona:dev"][0]),
    )


def test_known_good_artifacts_have_no_blocking_findings(script: dict[str, list[Any]]) -> None:
    brief, req, design, impl = _artifacts(script)
    assert not checks.blocking(checks.check_problem_brief(brief))
    assert not checks.blocking(checks.check_requirements(req, brief))
    assert not checks.blocking(checks.check_design(design))
    assert not checks.blocking(checks.check_implementation(impl, req))


def test_untraced_story_and_bad_goal_index_block(script: dict[str, list[Any]]) -> None:
    brief, _, _, _ = _artifacts(script)
    raw = script["persona:ba"][0]
    raw["traceability"] = raw["traceability"][:-1]
    raw["traceability"][0]["business_goal_indices"] = [9]
    messages = [
        f.message
        for f in checks.blocking(
            checks.check_requirements(RequirementsPackage.model_validate(raw), brief)
        )
    ]
    assert any("US-4 has no traceability" in m for m in messages)
    assert any("goal indices [9]" in m for m in messages)


def test_risk_without_mitigation_blocks(script: dict[str, list[Any]]) -> None:
    raw = script["persona:architect"][1]
    raw["risks"] = [{"description": "x", "mitigation": "TBD"}]
    found = checks.blocking(checks.check_design(TechnicalDesign.model_validate(raw)))
    assert len(found) == 2  # fewer than 2 risks + risk #1 without mitigation


def test_dev_syntax_error_and_silent_skip_block(script: dict[str, list[Any]]) -> None:
    _, req, _, _ = _artifacts(script)
    raw = script["persona:dev"][0]
    raw["files"][1]["content"] = "def broken(:\n"
    raw["stubbed_stories"] = raw["stubbed_stories"][:1]  # US-4 silently skipped
    messages = [
        f.message
        for f in checks.blocking(
            checks.check_implementation(Implementation.model_validate(raw), req)
        )
    ]
    assert any("does not parse" in m for m in messages)
    assert any("US-4 is neither implemented nor stubbed" in m for m in messages)


def test_uncovered_acceptance_criteria(script: dict[str, list[Any]]) -> None:
    _, req, _, _ = _artifacts(script)
    plan = TestPlan.model_validate(script["persona:qa"][0])
    assert checks.uncovered_acceptance_criteria(plan, req) == []
    raw = script["persona:qa"][0]
    raw["untestable_criteria"] = []
    assert "US-3-AC1" in checks.uncovered_acceptance_criteria(TestPlan.model_validate(raw), req)


def test_verifier_syntax_only_by_default(script: dict[str, list[Any]]) -> None:
    report = CodeVerifier(execute=False).verify(
        Implementation.model_validate(script["persona:dev"][0])
    )
    assert report.files_checked == 4
    assert report.syntax_errors == []
    assert not report.tests_executed
    assert report.skipped_reason and "disabled" in report.skipped_reason


def test_verifier_executes_generated_tests(script: dict[str, list[Any]]) -> None:
    report = CodeVerifier(execute=True, timeout_s=60).verify(
        Implementation.model_validate(script["persona:dev"][0])
    )
    assert report.tests_executed
    assert (report.tests_passed, report.tests_failed) == (3, 0), report.output_tail


def test_verifier_reports_real_failures(script: dict[str, list[Any]]) -> None:
    raw = script["persona:dev"][0]
    raw["files"][3]["content"] += "\n\ndef test_deliberately_wrong():\n    assert 1 == 2\n"
    report = CodeVerifier(execute=True, timeout_s=60).verify(Implementation.model_validate(raw))
    assert (report.tests_passed, report.tests_failed) == (3, 1)


@pytest.mark.parametrize("path", ["../escape.py", "/etc/evil.py"])
def test_verifier_refuses_path_traversal(script: dict[str, list[Any]], path: str) -> None:
    raw = script["persona:dev"][0]
    raw["files"][0]["path"] = path
    report = CodeVerifier(execute=True).verify(Implementation.model_validate(raw))
    assert not report.tests_executed
    assert report.skipped_reason and "outside sandbox" in report.skipped_reason
