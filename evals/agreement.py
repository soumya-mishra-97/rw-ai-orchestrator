"""Evaluator-vs-human agreement (spec Section 8: "the single most credible number").

    uv run python -m evals.agreement export evals/reports/<run-dir> --n 20
    #   → evals/labels/<run-dir>/sheet.csv + items/*.json  (blind: no evaluator verdicts)
    #   … a human fills the `human_verdict` column with pass / fail using docs/rubric …
    uv run python -m evals.agreement score evals/labels/<run-dir>

Export is *blind*: the sheet contains the artifact and its upstream context but
not the evaluator's scores or verdict, which are kept in ``key.json``. Scoring
reports raw agreement, Cohen's kappa (agreement corrected for chance) and the
confusion matrix — the false-PASS cell is the one that matters most.
"""

from __future__ import annotations

import csv
import json
import random
from pathlib import Path
from typing import Annotated

import typer

from evals.report import splice
from sdlc_loop.governance.audit import AuditEventType, AuditLog
from sdlc_loop.governance.pii import RegexRedactor

app = typer.Typer(add_completion=False)
ROOT = Path(__file__).resolve().parent.parent


def cohens_kappa(a: list[str], b: list[str]) -> float | None:
    if not a or len(a) != len(b):
        return None
    labels = sorted(set(a) | set(b))
    n = len(a)
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    expected = sum((a.count(lbl) / n) * (b.count(lbl) / n) for lbl in labels)
    if expected == 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return round((observed - expected) / (1 - expected), 3)


@app.command()
def export(
    report_dir: Path,
    n: Annotated[int, typer.Option(min=1)] = 20,
    seed: int = 7,
) -> None:
    """Sample judged handoffs from an eval run into a blind labeling sheet."""
    audit = AuditLog(report_dir / "audit.db", RegexRedactor())
    events = audit.events()
    items = []
    for i, ev in enumerate(events):
        if (
            ev.event_type is not AuditEventType.EVALUATION
            or ev.payload.get("judge") == "deterministic"
        ):
            continue
        # the artifact under review is the latest persona output for this run/persona before the gate
        produced = next(
            (e for e in reversed(events[:i])
             if e.run_id == ev.run_id and e.persona == ev.persona
             and e.event_type is AuditEventType.PERSONA_CALL and "output" in e.payload),
            None,
        )  # fmt: skip
        if produced is None:
            continue
        items.append((ev, produced.payload["output"]))
    random.Random(seed).shuffle(items)
    items = items[:n]
    target = ROOT / "evals" / "labels" / report_dir.name
    (target / "items").mkdir(parents=True, exist_ok=True)
    key = {}
    with (target / "sheet.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["item", "run_id", "handoff", "attempt", "artifact_file", "human_verdict", "notes"]
        )
        for idx, (ev, artifact) in enumerate(items, start=1):
            item = f"item-{idx:02d}"
            (target / "items" / f"{item}.json").write_text(json.dumps(artifact, indent=2), "utf-8")
            writer.writerow([item, ev.run_id, ev.handoff, ev.attempt, f"items/{item}.json", "", ""])
            key[item] = {"verdict": ev.verdict, "overall": ev.payload.get("overall_score"),
                         "judge": ev.payload.get("judge")}  # fmt: skip
    (target / "key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")
    typer.echo(f"{len(items)} items → {target}/sheet.csv (fill human_verdict with pass/fail)")


@app.command()
def score(label_dir: Path, update_docs: bool = False) -> None:
    """Compare filled-in human verdicts with the evaluator's (pass vs not-pass)."""
    key = json.loads((label_dir / "key.json").read_text(encoding="utf-8"))
    human: list[str] = []
    judge: list[str] = []
    with (label_dir / "sheet.csv").open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            verdict = (row.get("human_verdict") or "").strip().lower()
            if verdict not in {"pass", "fail"}:
                continue
            human.append(verdict)
            judge.append("pass" if key[row["item"]]["verdict"] == "pass" else "fail")
    if not human:
        typer.echo("no labeled rows yet")
        raise typer.Exit(1)
    agree = sum(h == j for h, j in zip(human, judge, strict=True))
    matrix = {
        f"human_{h}/judge_{j}": sum(
            1 for x, y in zip(human, judge, strict=True) if (x, y) == (h, j)
        )
        for h in ("pass", "fail")
        for j in ("pass", "fail")
    }
    result = {
        "labeled_items": len(human),
        "agreement_rate": round(agree / len(human), 3),
        "cohens_kappa": cohens_kappa(human, judge),
        "confusion": matrix,
        "false_pass (judge passed, human failed)": matrix["human_fail/judge_pass"],
    }
    (label_dir / "agreement.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    typer.echo(json.dumps(result, indent=2))
    if update_docs:
        md = (
            f"Source: `{label_dir.name}` — {len(human)} handoffs hand-labeled blind.\n\n"
            "| Metric | Value |\n|---|---|\n"
            f"| Raw agreement (pass vs not-pass) | {result['agreement_rate']:.0%} |\n"
            f"| Cohen's kappa | {result['cohens_kappa']} |\n"
            f"| Judge PASS, human FAIL (false pass) | {matrix['human_fail/judge_pass']} |\n"
            f"| Judge not-PASS, human PASS (false reject) | {matrix['human_pass/judge_fail']} |\n"
        )
        splice(ROOT / "docs" / "evaluation.md", "AGREEMENT_RESULTS", md)


if __name__ == "__main__":
    app()
