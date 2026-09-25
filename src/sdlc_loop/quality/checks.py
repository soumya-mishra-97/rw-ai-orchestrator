"""Deterministic handoff checks that run before the LLM judge.

Structural invariants (every story traced, risks mitigated, code parses …) are
checked in code: zero tokens, zero variance. A *blocking* finding sends the
artifact straight back for a retry without paying for a judge call; *advisory*
findings are passed to the judge as hints. Only unambiguous, mechanical rules
are blocking — anything that needs judgement stays with the evaluator.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Literal

from sdlc_loop.schemas.artifacts import (
    Implementation,
    ProblemBrief,
    RequirementsPackage,
    TechnicalDesign,
    TestPlan,
)

Severity = Literal["blocking", "advisory"]


@dataclass(frozen=True, slots=True)
class Finding:
    severity: Severity
    message: str


def _count(label: str, n: int, lo: int, hi: int) -> list[Finding]:
    if lo <= n <= hi:
        return []
    return [Finding("advisory", f"{label}: expected {lo}-{hi}, got {n}.")]


def check_problem_brief(brief: ProblemBrief) -> list[Finding]:
    findings = _count("business_goals", len(brief.business_goals), 3, 5)
    findings += _count("success_metrics", len(brief.success_metrics), 2, 4)
    findings += [
        Finding("advisory", f"success metric has no number, may be vague: {m!r}")
        for m in brief.success_metrics
        if not re.search(r"\d", m)
    ]
    return findings


def check_requirements(req: RequirementsPackage, brief: ProblemBrief) -> list[Finding]:
    findings = _count("user_stories", len(req.user_stories), 4, 8)
    story_ids = {s.id for s in req.user_stories}
    traced = {t.story_id for t in req.traceability}
    for sid in sorted(story_ids - traced):
        findings.append(Finding("blocking", f"story {sid} has no traceability entry."))
    for sid in sorted(traced - story_ids):
        findings.append(Finding("blocking", f"traceability references unknown story {sid}."))
    n_goals = len(brief.business_goals)
    for link in req.traceability:
        bad = [i for i in link.business_goal_indices if not 0 <= i < n_goals]
        if bad:
            findings.append(
                Finding(
                    "blocking",
                    f"story {link.story_id} traces to goal indices {bad}, "
                    f"but the brief has goals 0-{n_goals - 1}.",
                )
            )
    for story in req.user_stories:
        findings += _count(f"{story.id} acceptance_criteria", len(story.acceptance_criteria), 2, 4)
    return findings


def check_design(design: TechnicalDesign) -> list[Finding]:
    findings: list[Finding] = []
    if len(design.risks) < 2:
        findings.append(Finding("blocking", f"at least 2 risks required, got {len(design.risks)}."))
    for i, risk in enumerate(design.risks):
        if len(risk.mitigation.strip()) < 10:
            findings.append(Finding("blocking", f"risk #{i + 1} has no concrete mitigation."))
    findings += _count("adrs", len(design.adrs), 2, 4)
    if len(design.integration_approach.strip()) < 40:
        findings.append(Finding("blocking", "integration_approach is missing or too thin."))
    return findings


def python_syntax_errors(impl: Implementation) -> list[str]:
    errors: list[str] = []
    for f in impl.files:
        if f.language.lower() != "python" and not f.path.endswith(".py"):
            continue
        try:
            ast.parse(f.content, filename=f.path)
        except SyntaxError as exc:
            errors.append(f"{f.path}:{exc.lineno}: {exc.msg}")
    return errors


def check_implementation(impl: Implementation, req: RequirementsPackage) -> list[Finding]:
    story_ids = {s.id for s in req.user_stories}
    implemented = set(impl.implemented_story_ids)
    stubbed = {s.story_id for s in impl.stubbed_stories}
    findings = [
        Finding("blocking", f"generated code does not parse: {e}")
        for e in python_syntax_errors(impl)
    ]
    for sid in sorted((implemented | stubbed) - story_ids):
        findings.append(Finding("blocking", f"references unknown story {sid}."))
    for sid in sorted(story_ids - implemented - stubbed):
        findings.append(
            Finding(
                "blocking", f"story {sid} is neither implemented nor stubbed (silently skipped)."
            )
        )
    findings += _count("implemented stories", len(implemented), 1, 2)
    return findings


def uncovered_acceptance_criteria(plan: TestPlan, req: RequirementsPackage) -> list[str]:
    """ACs with no test case and not declared untestable (QA coverage metric)."""
    covered = {ac for tc in plan.test_cases for ac in tc.acceptance_criterion_ids}
    declared = set(plan.untestable_criteria)
    return sorted(req.acceptance_criterion_ids() - covered - declared)


def blocking(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == "blocking"]
