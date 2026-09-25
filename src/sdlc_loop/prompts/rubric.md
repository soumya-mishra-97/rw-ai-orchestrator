# Handoff rubric (used by the Evaluator; visible to every agent)

Every handoff is scored on five dimensions, each an integer from 1 to 5.

| Score | Meaning |
|---|---|
| 5 | Excellent. Nothing material to fix. |
| 4 | Good. Minor polish only; the next stage can act on it without guessing. |
| 3 | Adequate but with a real gap the next stage would have to work around. |
| 2 | Weak. Several material gaps or one serious error. |
| 1 | Unacceptable. Contradicts a constraint, is unbuildable, or is missing a required part entirely. |

## Dimension anchors

**completeness** — Does the artifact contain everything its stage requires?
- 5: every required section present and substantive (e.g. Architect: components, integration approach, data flow, ≥2 risks with mitigations, 2-4 ADRs).
- 3: one required element thin or missing.
- 1: a whole required section missing or empty.

**correctness_feasibility** — Is it technically and logically sound and buildable with what actually exists?
- 5: every mechanism named is real and would work under the stated constraints.
- 3: workable, but one step is hand-waved or unclear how it would work.
- 1: relies on a capability that does not exist (e.g. calling an API the system does not have), or code that cannot run.

**legacy_constraint_awareness** — Does it explicitly respect every constraint carried from the ProblemBrief?
- 5: each legacy/vendor/compliance constraint is visibly honoured and the design/requirements are shaped by it.
- 3: constraints respected but only implicitly; one is not addressed where it clearly should be.
- 1: any legacy or vendor constraint silently dropped or contradicted (e.g. "real-time hook into the batch-only mainframe", "enable SAML in the vendor suite that has no SAML support"). A score of 1 here is a critical failure.

**clarity_actionability** — Could the next persona act on this without guessing?
- 5: unambiguous, specific, uses stable ids.
- 3: understandable but the next stage must make at least one assumption.
- 1: vague or internally contradictory.

**traceability** — Can each new item be traced back to an upstream goal, requirement or acceptance criterion?
- 5: every item maps to an upstream id or goal, explicitly.
- 3: most items trace; a few are orphaned or the mapping is implicit.
- 1: items cannot be tied to anything upstream.

## Stage-specific expectations

- **PM→BA (scores the ProblemBrief):** 3-5 measurable goals; 2-4 metrics with numbers; every constraint in the request captured without softening.
- **BA→Architect (scores the RequirementsPackage):** 4-8 stories, 2-4 testable acceptance criteria each; every story traced to a goal index; NFRs consistent with the constraints; conflicts surfaced as open questions.
- **Architect→Dev (scores the TechnicalDesign):** integration approach names the concrete mechanism for every legacy/vendor system; no invented live APIs; ≥2 risks each with a concrete mitigation; 2-4 ADRs with alternatives.
- **Dev→QA (scores the Implementation):** real code for 1-2 stories that follows the integration approach; every other story stubbed honestly; deviations stated explicitly; the legacy system is mocked behind the designed adapter rather than bypassed.

## Gate policy (applied by the harness, not by the model)

overall = mean of the five scores. PASS requires overall ≥ 4.0, no dimension below 3, and no critical flag. Any dimension scored 1 raises the critical flag.
