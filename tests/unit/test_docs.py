"""The eval scripts splice results into docs at the very end of a paid run — make
sure every marker they need exists so a live eval can never fail at the last step."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.report import splice
from tests.conftest import ROOT

MARKERS = [
    ("docs/evaluation.md", "EVAL_RESULTS"),
    ("docs/evaluation.md", "JUDGE_RESULTS"),
    ("docs/evaluation.md", "AGREEMENT_RESULTS"),
    ("docs/cost-optimization.md", "COST_RESULTS"),
    ("README.md", "HEADLINE"),
]


@pytest.mark.parametrize(("path", "marker"), MARKERS)
def test_splice_markers_exist(path: str, marker: str) -> None:
    text = (ROOT / path).read_text(encoding="utf-8")
    assert f"<!-- BEGIN:{marker} -->" in text and f"<!-- END:{marker} -->" in text


def test_splice_replaces_only_between_markers(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("intro\n<!-- BEGIN:X -->\nold\n<!-- END:X -->\noutro\n", encoding="utf-8")
    splice(doc, "X", "new table")
    assert (
        doc.read_text(encoding="utf-8")
        == "intro\n<!-- BEGIN:X -->\nnew table\n<!-- END:X -->\noutro\n"
    )
