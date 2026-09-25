"""Labeled judge benchmark built on Scenario 1 (spec Section 7 anti-pattern check, generalised).

Each case puts one artifact in front of the evaluator with known-good upstream
context. ``expected="fail"`` cases contain exactly one seeded defect of the kind
the brief cares most about (a legacy/vendor constraint silently violated);
``expected="pass"`` cases are the known-good fixture artifacts. The evaluator's
job is to reject every bad case and pass every good one.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Literal

from sdlc_loop.demo.library import example_script
from sdlc_loop.schemas.common import Handoff


@dataclass(frozen=True, slots=True)
class FaultCase:
    id: str
    handoff: Handoff
    artifact: dict[str, Any]
    expected: Literal["pass", "fail"]
    defect: str


def _fixture() -> dict[str, Any]:
    script = example_script("fraud-mainframe")
    return {
        "brief": script["persona:pm"][0],
        "requirements": script["persona:ba"][0],
        "flawed_design": script["persona:architect"][0],
        "design": script["persona:architect"][1],
        "implementation": script["persona:dev"][0],
    }


def upstream() -> dict[str, Any]:
    """Known-good upstream artifacts every case is evaluated against."""
    return _fixture()


def build_cases() -> list[FaultCase]:
    f = _fixture()

    brief_dropped = copy.deepcopy(f["brief"])
    brief_dropped["constraints"] = ["Compliance requires every fraud decision to be logged."]
    brief_dropped["business_goals"][0] = (
        "Block fraudulent card transactions in real time at authorisation."
    )

    reqs_realtime = copy.deepcopy(f["requirements"])
    reqs_realtime["non_functional_requirements"][0] = (
        "Performance: each transaction is scored within 100 ms at authorisation time via the "
        "payments engine API, and declined immediately if high risk."
    )
    reqs_realtime["open_questions"] = reqs_realtime["open_questions"][1:]

    design_cobol = copy.deepcopy(f["design"])
    design_cobol["integration_approach"] = (
        "Modify the COBOL settlement program to call the Fraud Scoring Engine over MQ for each "
        "record during the batch run and skip settlement for high-risk records. The mainframe "
        "team will add the MQ client call to the settlement copybook."
    )

    impl_bypass = copy.deepcopy(f["implementation"])
    impl_bypass["files"][2]["content"] += (
        "\n\nimport json\nimport urllib.request\n\n\n"
        "def block_on_mainframe(txn_id):\n"
        "    req = urllib.request.Request('https://mainframe.internal/api/v1/transactions/'"
        " + txn_id + '/block', data=json.dumps({'reason': 'fraud'}).encode(), method='POST')\n"
        "    return urllib.request.urlopen(req).status\n"
    )
    impl_bypass["summary"] = (
        "Scores transactions and blocks high-risk ones immediately via the mainframe REST API."
    )

    design_no_mitigation = copy.deepcopy(f["design"])
    design_no_mitigation["risks"] = [
        {"description": "Scoring may not finish before cutoff.", "mitigation": "TBD"},
        {"description": "Extract format may change.", "mitigation": ""},
    ]

    return [
        FaultCase("G1-brief", Handoff.PM_TO_BA, f["brief"], "pass", "known-good brief"),
        FaultCase("G2-requirements", Handoff.BA_TO_ARCHITECT, f["requirements"], "pass", "known-good"),
        FaultCase("G3-design", Handoff.ARCHITECT_TO_DEV, f["design"], "pass", "known-good design"),
        FaultCase("G4-implementation", Handoff.DEV_TO_QA, f["implementation"], "pass", "known-good"),
        FaultCase("F1-brief-drops-constraints", Handoff.PM_TO_BA, brief_dropped, "fail",
                  "PM drops batch-only / no-API / no-vendor-dev constraints and promises real-time blocking"),
        FaultCase("F2-reqs-assume-realtime", Handoff.BA_TO_ARCHITECT, reqs_realtime, "fail",
                  "BA writes a 100 ms authorisation-time NFR against a batch-only mainframe and deletes the conflict question"),
        FaultCase("F3-design-rest-webhook", Handoff.ARCHITECT_TO_DEV, f["flawed_design"], "fail",
                  "Architect assumes a webhook + REST API on a mainframe that has neither (spec anti-pattern)"),
        FaultCase("F4-design-modifies-cobol", Handoff.ARCHITECT_TO_DEV, design_cobol, "fail",
                  "Architect modifies COBOL code although the vendor offers no custom development"),
        FaultCase("F5-impl-bypasses-adapter", Handoff.DEV_TO_QA, impl_bypass, "fail",
                  "Dev silently adds a call to a non-existent mainframe REST API"),
        FaultCase("F6-design-no-mitigations", Handoff.ARCHITECT_TO_DEV, design_no_mitigation, "fail",
                  "Risks without mitigations (should be caught by the deterministic pre-gate)"),
    ]  # fmt: skip
