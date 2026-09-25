"""SDLC Execution Report: renders a :class:`RunView` as a print-ready PDF.

The report reads only the view the browser already shows (built from the audit
log and checkpoint state), so every number in it matches the dashboard.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Flowable,
    Frame,
    KeepTogether,
    ListFlowable,
    ListItem,
    NextPageTemplate,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)

from sdlc_loop.orchestrator.view import AttemptView, EvaluationView, RunView, StageView
from sdlc_loop.reports.fonts import FontSet, report_fonts
from sdlc_loop.schemas.evaluation import Dimension

BRAND = "Robert Walters AI-Orchestrator"
REPORT_TITLE = "SDLC Execution Report"

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
BAND_H = 46 * mm
CONTENT_W = PAGE_W - 2 * MARGIN

NAVY = colors.HexColor("#1B2A41")
ACCENT = colors.HexColor("#2F5D9A")
INK = colors.HexColor("#1F2328")
MUTED = colors.HexColor("#5B6573")
RULE = colors.HexColor("#D0D7E2")
ZEBRA = colors.HexColor("#F4F6FA")
SHADE = colors.HexColor("#EEF2F8")
TONES = {
    "success": (colors.HexColor("#1E7B45"), colors.HexColor("#E8F5EE")),
    "warning": (colors.HexColor("#B45309"), colors.HexColor("#FDF3E7")),
    "danger": (colors.HexColor("#B42318"), colors.HexColor("#FCEBEA")),
    "info": (colors.HexColor("#1D4ED8"), colors.HexColor("#EAF0FD")),
    "human": (colors.HexColor("#6D28D9"), colors.HexColor("#F1ECFD")),
    "neutral": (MUTED, colors.HexColor("#F1F3F6")),
}
STATUS_TONE = {
    "complete": "success",
    "passed": "success",
    "completed": "success",
    "running": "info",
    "queued": "info",
    "retrying": "warning",
    "blocked_for_human": "danger",
    "awaiting_review": "danger",
    "failed": "danger",
    "aborted": "danger",
    "rejected": "danger",
    "accepted_by_human": "human",
    "pending": "neutral",
}
DECISION_TONE = {
    "PASS": "success",
    "RETRY": "warning",
    "ESCALATE": "danger",
    "ACCEPT_AS_IS": "human",
    "RETRY_WITH_GUIDANCE": "human",
    "ABORT": "danger",
}
SOURCE_LABEL = {
    "claude": "Live (Claude API)",
    "example": "Demo: curated example",
    "simulator": "Demo: local simulator",
}
SOURCE_NOTE = {
    "claude": "Live run: tokens and cost are as reported by the Claude API.",
    "example": (
        "Curated example: the agents replayed hand-authored outputs; token and cost figures "
        "are estimates."
    ),
    "simulator": (
        "Simulated run: agents filled templates and a rule-based judge scored them; no model "
        "was called."
    ),
}
DIMENSION_ABBR = {
    Dimension.COMPLETENESS: "CMP",
    Dimension.CORRECTNESS_FEASIBILITY: "COR",
    Dimension.LEGACY_CONSTRAINT_AWARENESS: "CON",
    Dimension.CLARITY_ACTIONABILITY: "CLA",
    Dimension.TRACEABILITY: "TRC",
}


def render_run_report(
    view: RunView, *, generated_at: datetime | None = None, fonts: FontSet | None = None
) -> bytes:
    """Return the PDF bytes of the SDLC Execution Report for one run."""
    return _Report(view, fonts or report_fonts(), generated_at or datetime.now(UTC)).build()


def report_filename(run_id: str) -> str:
    return f"{run_id}-sdlc-execution-report.pdf"


# --------------------------------------------------------------------------- formatting
def _when(ts: str | None, *, time_only: bool = False) -> str:
    if not ts:
        return "—"
    moment = datetime.fromisoformat(ts).astimezone(UTC)
    return moment.strftime("%H:%M:%S") if time_only else moment.strftime("%d %b %Y, %H:%M:%S UTC")


def _duration(start: str | None, end: str | None) -> str:
    if not start or not end:
        return "—"
    seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    return f"{int(seconds // 60)} min {int(seconds % 60)} s"


def _usd(value: float) -> str:
    return f"${value:,.4f}"


def _num(value: int) -> str:
    return f"{value:,}"


def _score(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _title(key: str) -> str:
    return key.replace("_", " ").capitalize()


def _status_label(status: str) -> str:
    return {"blocked_for_human": "Awaiting human review"}.get(status, _title(status))


# --------------------------------------------------------------------------- page chrome
class _NumberedCanvas(Canvas):
    """Defers page output so every footer can say "Page n of N"."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pages: list[dict[str, Any]] = []
        self.footer: Callable[[Canvas, int, int], None] | None = None

    def showPage(self) -> None:  # noqa: N802 — reportlab API
        self._pages.append(dict(self.__dict__))
        self._startPage()  # type: ignore[attr-defined]

    def save(self) -> None:
        total = len(self._pages)
        for page in self._pages:
            self.__dict__.update(page)
            if self.footer is not None:
                self.footer(self, self._pageNumber, total)  # type: ignore[attr-defined]
            super().showPage()
        super().save()


class _Report:
    def __init__(self, view: RunView, fonts: FontSet, generated_at: datetime) -> None:
        self.view = view
        self.fonts = fonts
        self.generated_at = generated_at
        self.styles = self._styles()
        self.section = 0

    # ------------------------------------------------------------------ build
    def build(self) -> bytes:
        buffer = io.BytesIO()
        doc = BaseDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=MARGIN,
            rightMargin=MARGIN,
            topMargin=MARGIN,
            bottomMargin=MARGIN,
            title=self.fonts.clean(f"{REPORT_TITLE}: {self.view.title}"),
            author=BRAND,
            subject=f"Run {self.view.run_id}",
            creator=BRAND,
        )
        body_h = PAGE_H - 2 * MARGIN - 10 * mm
        cover = Frame(MARGIN, MARGIN, CONTENT_W, PAGE_H - BAND_H - MARGIN - 6 * mm, id="cover")
        body = Frame(MARGIN, MARGIN + 4 * mm, CONTENT_W, body_h, id="body")
        doc.addPageTemplates(
            [
                PageTemplate("cover", [cover], onPage=self._draw_cover_band),
                PageTemplate("body", [body], onPage=self._draw_header),
            ]
        )

        def canvasmaker(*args: Any, **kwargs: Any) -> _NumberedCanvas:
            canvas = _NumberedCanvas(*args, **kwargs)
            canvas.footer = self._draw_footer
            return canvas

        doc.build(self._story(), canvasmaker=canvasmaker)
        return buffer.getvalue()

    def _story(self) -> list[Flowable]:
        story: list[Flowable] = [NextPageTemplate("body")]
        story += self._overview()
        story += self._summary()
        story += self._requirement()
        story += self._plan()
        story += self._scorecard()
        story += self._decisions()
        story += self._deliverables()
        story += self._costs()
        story += self._timeline()
        story.append(Spacer(1, 6 * mm))
        story.append(
            self._p(
                f"Generated by {BRAND} from the run's tamper-evident audit log and workflow "
                "checkpoint. Every score, retry and decision above is recorded there with its "
                "timestamp.",
                "note",
            )
        )
        return story

    # ------------------------------------------------------------------ canvas chrome
    def _draw_cover_band(self, canvas: Canvas, _doc: BaseDocTemplate) -> None:
        f = self.fonts
        canvas.saveState()
        canvas.setFillColor(NAVY)
        canvas.rect(0, PAGE_H - BAND_H, PAGE_W, BAND_H, stroke=0, fill=1)
        canvas.setFillColor(ACCENT)
        canvas.rect(0, PAGE_H - BAND_H, PAGE_W, 1.6 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.HexColor("#B8C4D6"))
        canvas.setFont(f.serif_bold, 10.5)
        canvas.drawString(MARGIN, PAGE_H - 15 * mm, BRAND.upper())
        canvas.setFillColor(colors.white)
        canvas.setFont(f.serif_bold, 26)
        canvas.drawString(MARGIN, PAGE_H - 28 * mm, REPORT_TITLE)
        canvas.setFont(f.serif, 11)
        canvas.setFillColor(colors.HexColor("#D5DDE9"))
        canvas.drawString(
            MARGIN,
            PAGE_H - 37 * mm,
            f.clean(
                "Multi-agent SDLC workflow with evaluator gates · Generated "
                f"{self.generated_at.strftime('%d %b %Y, %H:%M UTC')}"
            ),
        )
        canvas.restoreState()

    def _draw_header(self, canvas: Canvas, _doc: BaseDocTemplate) -> None:
        f = self.fonts
        canvas.saveState()
        top = PAGE_H - MARGIN + 2 * mm
        canvas.setFont(f.serif_bold, 9)
        canvas.setFillColor(NAVY)
        canvas.drawString(MARGIN, top, BRAND)
        canvas.setFont(f.serif, 9)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(
            PAGE_W - MARGIN, top, f.clean(f"{REPORT_TITLE} · {self.view.run_id}")
        )
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.6)
        canvas.line(MARGIN, top - 2.5 * mm, PAGE_W - MARGIN, top - 2.5 * mm)
        canvas.restoreState()

    def _draw_footer(self, canvas: Canvas, page: int, total: int) -> None:
        f = self.fonts
        canvas.saveState()
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.6)
        canvas.line(MARGIN, MARGIN, PAGE_W - MARGIN, MARGIN)
        canvas.setFont(f.serif, 8.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(
            MARGIN,
            MARGIN - 4.5 * mm,
            f.clean(f"{BRAND} · Generated {self.generated_at.strftime('%d %b %Y, %H:%M UTC')}"),
        )
        canvas.drawRightString(PAGE_W - MARGIN, MARGIN - 4.5 * mm, f"Page {page} of {total}")
        canvas.restoreState()

    # ------------------------------------------------------------------ styles & primitives
    def _styles(self) -> dict[str, ParagraphStyle]:
        f = self.fonts
        base = ParagraphStyle("base", fontName=f.serif, fontSize=10.5, leading=14, textColor=INK)

        def make(name: str, **kw: Any) -> ParagraphStyle:
            return ParagraphStyle(name, parent=base, **kw)

        return {
            "body": base,
            "title": make("title", fontName=f.serif_bold, fontSize=19, leading=24, spaceAfter=2),
            "h1": make(
                "h1",
                fontName=f.serif_bold,
                fontSize=15,
                leading=19,
                textColor=NAVY,
                spaceBefore=12,
                spaceAfter=6,
            ),
            "h2": make(
                "h2",
                fontName=f.serif_bold,
                fontSize=12.5,
                leading=16,
                textColor=ACCENT,
                spaceBefore=10,
                spaceAfter=4,
                keepWithNext=1,
            ),
            "h3": make(
                "h3",
                fontName=f.serif_bold,
                fontSize=10.5,
                leading=14,
                spaceBefore=6,
                spaceAfter=2,
                keepWithNext=1,
            ),
            "muted": make("muted", textColor=MUTED, fontSize=9.5, leading=12.5),
            "note": make(
                "note", fontName=f.serif_italic, textColor=MUTED, fontSize=9.5, leading=12.5
            ),
            "cell": make("cell", fontSize=9, leading=11.5),
            "cell_b": make("cell_b", fontName=f.serif_bold, fontSize=9, leading=11.5),
            "cell_c": make("cell_c", fontSize=9, leading=11.5, alignment=TA_CENTER),
            "th": make(
                "th", fontName=f.serif_bold, fontSize=9, leading=11.5, textColor=colors.white
            ),
            "th_c": make(
                "th_c",
                fontName=f.serif_bold,
                fontSize=9,
                leading=11.5,
                textColor=colors.white,
                alignment=TA_CENTER,
            ),
            "kpi": make(
                "kpi",
                fontName=f.serif_bold,
                fontSize=16,
                leading=19,
                textColor=NAVY,
                alignment=TA_CENTER,
            ),
            "kpi_label": make(
                "kpi_label", fontSize=8.5, leading=10.5, textColor=MUTED, alignment=TA_CENTER
            ),
            "headline": make("headline", fontName=f.serif_bold, fontSize=12, leading=15.5),
            "code": ParagraphStyle(
                "code", fontName=f.mono, fontSize=7.2, leading=8.9, textColor=INK
            ),
            "path": make(
                "path",
                fontName=f.mono_bold,
                fontSize=8.5,
                leading=11,
                spaceBefore=6,
                spaceAfter=2,
                keepWithNext=1,
            ),
        }

    def _p(self, text: str, style: str = "body", *, markup: bool = False) -> Paragraph:
        cleaned = self.fonts.clean(text)
        return Paragraph(cleaned if markup else escape(cleaned), self.styles[style])

    def _lead(self, label: str, text: str, style: str = "body") -> Paragraph:
        """A paragraph that starts with a bold label."""
        return self._p(
            f"<b>{escape(label)}</b> {escape(self.fonts.clean(text))}", style, markup=True
        )

    def _tone_text(self, text: str, tone: str, style: str = "cell_b") -> Paragraph:
        color = TONES.get(tone, TONES["neutral"])[0].hexval()[2:]
        return self._p(
            f'<font color="#{color}">{escape(self.fonts.clean(text))}</font>', style, markup=True
        )

    def _heading(self, text: str) -> list[Flowable]:
        self.section += 1
        return [CondPageBreak(40 * mm), self._p(f"{self.section}. {text}", "h1")]

    def _bullets(self, items: Iterable[str], style: str = "body") -> Flowable | None:
        rows = [ListItem(self._p(item, style), leftIndent=12) for item in items if item]
        if not rows:
            return None
        return ListFlowable(
            rows,  # type: ignore[arg-type]  # stubs omit that ListItem is a flowable
            bulletType="bullet",
            start="•" if self.fonts.glyphs else "-",
            leftIndent=12,
            bulletFontSize=10,
            bulletColor=ACCENT,
        )

    def _labelled_bullets(self, label: str, items: Sequence[str]) -> list[Flowable]:
        block = self._bullets(items)
        return [self._p(label, "h3"), block] if block is not None else []

    def _table(
        self,
        header: Sequence[str],
        rows: Sequence[Sequence[object]],
        widths: Sequence[float],
        *,
        center: Sequence[int] = (),
        extra: Sequence[tuple[Any, ...]] = (),
    ) -> Table:
        head = [self._p(h, "th_c" if i in center else "th") for i, h in enumerate(header)]
        body = [
            [
                c if not isinstance(c, str) else self._p(c, "cell_c" if i in center else "cell")
                for i, c in enumerate(row)
            ]
            for row in rows
        ]
        table = Table([head, *body], colWidths=[w * CONTENT_W for w in widths], repeatRows=1)
        style: list[tuple[Any, ...]] = [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
            ("BOX", (0, 0), (-1, -1), 0.6, RULE),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]
        style += [("BACKGROUND", (0, r), (-1, r), ZEBRA) for r in range(2, len(body) + 1, 2)]
        table.setStyle(TableStyle([*style, *extra]))
        return table

    def _callout(self, content: Sequence[Flowable], tone: str) -> Table:
        edge, fill = TONES.get(tone, TONES["neutral"])
        box = Table([[list(content)]], colWidths=[CONTENT_W])
        box.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), fill),
                    ("LINEBEFORE", (0, 0), (0, -1), 3, edge),
                    ("LEFTPADDING", (0, 0), (-1, -1), 9),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )
        return box

    # ------------------------------------------------------------------ sections
    def _overview(self) -> list[Flowable]:
        v = self.view
        status_tone = STATUS_TONE.get(v.status, "neutral")
        meta: list[tuple[str, str | Paragraph]] = [
            ("Run ID", v.run_id),
            ("Status", self._tone_text(_status_label(v.status), status_tone)),
            ("Execution source", SOURCE_LABEL.get(v.source or "", "—")),
            ("Duration", _duration(v.started_at, v.finished_at)),
            ("Started", _when(v.started_at)),
            ("Finished", _when(v.finished_at)),
        ]
        cells = [
            [self._p(k, "muted"), val if isinstance(val, Flowable) else self._p(val, "cell_b")]
            for k, val in meta
        ]
        grid = Table(
            [cells[i] + cells[i + 1] for i in range(0, len(cells), 2)],
            colWidths=[w * CONTENT_W for w in (0.16, 0.34, 0.18, 0.32)],
        )
        grid.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        out: list[Flowable] = [
            self._p("Feature requirement", "muted"),
            self._p(v.title, "title"),
            Spacer(1, 3 * mm),
            grid,
            Spacer(1, 5 * mm),
        ]
        final = v.final_result
        tone: str
        if final is not None:
            headline, tone = final.headline, final.tone
        elif v.pending_review is not None:
            headline, tone = "Paused: an evaluator gate escalated this handoff to a human", "danger"
        elif v.error:
            headline, tone = f"Run did not finish: {v.error}", "danger"
        else:
            headline, tone = f"Run status: {_status_label(v.status)}", status_tone
        out.append(self._callout([self._p(headline, "headline")], tone))
        out.append(Spacer(1, 5 * mm))
        out.append(self._kpis())
        return out

    def _kpis(self) -> Table:
        v, t = self.view, self.view.totals
        gates = (
            f"{v.final_result.gates_passed}/{v.final_result.gates_total}"
            if v.final_result
            else f"{sum(s.status in ('passed', 'accepted_by_human') for s in v.stages)}/"
            f"{sum(1 for s in v.stages if s.handoff)}"
        )
        tiles = [
            (gates, "Gates passed"),
            (str(t.retries), "Retries"),
            (str(t.escalations), "Escalations"),
            (str(t.llm_calls), "LLM calls"),
            (
                f"{(t.input_tokens + t.output_tokens) / 1000:.1f}k",
                f"Tokens ({t.input_tokens / 1000:.1f}k in / {t.output_tokens / 1000:.1f}k out)",
            ),
            (_usd(t.cost_usd), "Total cost"),
        ]
        row = [[self._p(value, "kpi"), self._p(label, "kpi_label")] for value, label in tiles]
        table = Table([row], colWidths=[CONTENT_W / len(tiles)] * len(tiles))
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), SHADE),
                    ("BOX", (0, 0), (-1, -1), 0.6, RULE),
                    ("LINEAFTER", (0, 0), (-2, -1), 0.6, colors.white),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )
        return table

    def _summary(self) -> list[Flowable]:
        v = self.view
        out = self._heading("Executive Summary")
        final = v.final_result
        if final is not None:
            block = self._bullets(final.highlights)
            if block is not None:
                out.append(block)
            execution = final.test_execution
            if execution:
                if execution.get("tests_executed"):
                    text = (
                        f"Generated tests executed: {execution.get('tests_passed', 0)} passed, "
                        f"{execution.get('tests_failed', 0)} failed."
                    )
                else:
                    text = (
                        f"Generated code syntax-checked ({execution.get('files_checked', 0)} "
                        f"files); tests not executed: {execution.get('skipped_reason') or 'n/a'}."
                    )
                out += [Spacer(1, 2 * mm), self._p(text, "muted")]
        else:
            out.append(self._p("The run has not produced a final result yet.", "muted"))
        if v.pending_review is not None:
            review = v.pending_review
            out += [
                Spacer(1, 3 * mm),
                self._callout(
                    [
                        self._p(
                            f"Awaiting human review after {review.attempts_so_far} attempt(s) "
                            f"(score {_score(review.overall_score)}).",
                            "cell_b",
                        ),
                        self._p(review.evaluator_feedback, "cell"),
                    ],
                    "danger",
                ),
            ]
        if note := SOURCE_NOTE.get(v.source or ""):
            out += [Spacer(1, 2 * mm), self._p(note, "note")]
        return out

    def _requirement(self) -> list[Flowable]:
        v = self.view
        out = self._heading("Requirement & Orchestrator Validation")
        out.append(self._callout([self._p(v.requirement)], "neutral"))
        assessment = v.assessment or {}
        checks = assessment.get("checks") or []
        if checks:
            out += [Spacer(1, 3 * mm), self._p("Validation checks", "h3")]
            rows = [
                [
                    _title(str(c.get("name", ""))),
                    self._tone_text(
                        "Passed" if c.get("passed") else "Failed",
                        "success" if c.get("passed") else "danger",
                    ),
                    str(c.get("detail", "")),
                ]
                for c in checks
            ]
            out.append(self._table(["Check", "Result", "Detail"], rows, (0.22, 0.12, 0.66)))
        out += self._labelled_bullets("Reasons", [str(r) for r in assessment.get("reasons", [])])
        out += self._labelled_bullets(
            "Clarifications requested", [str(c) for c in assessment.get("clarifications", [])]
        )
        return out

    def _plan(self) -> list[Flowable]:
        plan = self.view.plan
        if not plan:
            return []
        out = self._heading("Workflow Plan")
        rows = [
            [
                str(s.get("order", "")),
                self._p(
                    f"<b>{escape(str(s.get('agent', '')))}</b><br/>"
                    f"{escape(self.fonts.clean(str(s.get('responsibility', ''))))}",
                    "cell",
                    markup=True,
                ),
                self._p(
                    f"{escape(str(s.get('model', '')))}<br/>"
                    f'<font color="#{MUTED.hexval()[2:]}">{s.get("model_tier", "")}</font>',
                    "cell",
                    markup=True,
                ),
                f"{s.get('receives', '')} → {s.get('produces', '')}",
                s.get("gate_label") or "Not gated",
            ]
            for s in plan.get("steps", [])
        ]
        out.append(
            self._table(
                ["#", "Agent", "Model", "Input → Output", "Gate"],
                rows,
                (0.04, 0.31, 0.24, 0.27, 0.14),
                center=(0,),
            )
        )
        out.append(Spacer(1, 2 * mm))
        out.append(
            self._p(
                f"Evaluator: {plan.get('evaluator_model', '—')}, confirmed by "
                f"{plan.get('evaluator_confirmation_model', '—')} when borderline. Pass rule: "
                f"{plan.get('pass_rule', '—')}. Up to {plan.get('max_attempts_per_handoff', '—')} "
                "attempts per handoff before escalation to a human.",
                "muted",
            )
        )
        return out

    def _scorecard(self) -> list[Flowable]:
        ev = self.view.evaluator
        out = self._heading("Evaluator Scorecard")
        if not ev.evaluations:
            return [*out, self._p("No handoff has been evaluated yet.", "muted")]
        dims = list(Dimension)
        rows: list[list[object]] = []
        extra: list[tuple[Any, ...]] = []
        for r, e in enumerate(ev.evaluations, start=1):
            scores = e.dimension_scores or {}
            cells: list[object] = []
            for c, d in enumerate(dims, start=4):
                value = scores.get(d.value)
                cells.append("—" if value is None else str(value))
                if value is not None and value <= 2:
                    extra.append(("BACKGROUND", (c, r), (c, r), TONES["danger"][1]))
            verdict = e.verdict.upper() + (" · CRITICAL" if e.critical_flag else "")
            rows.append(
                [
                    e.handoff_label,
                    str(e.attempt),
                    self._tone_text(verdict, _verdict_tone(e.verdict)),
                    _score(e.overall_score),
                    *cells,
                    e.judge + (" + tier2" if e.judge_escalated else ""),
                ]
            )
        out.append(
            self._table(
                ["Gate", "Att.", "Verdict", "Score", *(DIMENSION_ABBR[d] for d in dims), "Judge"],
                rows,
                (0.17, 0.06, 0.19, 0.08, 0.07, 0.07, 0.07, 0.07, 0.07, 0.15),
                center=(1, 3, 4, 5, 6, 7, 8),
                extra=extra,
            )
        )
        legend = ", ".join(f"{DIMENSION_ABBR[d]} = {d.label}" for d in dims)
        out += [
            Spacer(1, 2 * mm),
            self._p(f"Dimensions scored 1–5: {legend}. Scores of 2 or lower are shaded.", "muted"),
            self._p(
                f"{ev.judge_calls} judge call(s), {ev.tier2_confirmations} Tier-2 confirmation(s), "
                f"{ev.deterministic_rejections} deterministic pre-gate rejection(s); evaluator "
                f"used {_num(ev.input_tokens)} input / {_num(ev.output_tokens)} output tokens "
                f"({_usd(ev.cost_usd)}).",
                "muted",
            ),
        ]
        return out

    def _decisions(self) -> list[Flowable]:
        decisions = self.view.decisions
        if not decisions:
            return []
        out = self._heading("Governance Decisions")
        rows = [
            [
                _when(d.ts, time_only=True),
                d.handoff_label,
                str(d.attempt),
                self._tone_text(
                    d.decision.replace("_", " "), DECISION_TONE.get(d.decision, d.tone)
                ),
                d.decided_by,
                d.reason,
            ]
            for d in decisions
        ]
        out.append(
            self._table(
                ["Time", "Gate", "Att.", "Decision", "Decided by", "Reason"],
                rows,
                (0.09, 0.14, 0.06, 0.14, 0.2, 0.37),
                center=(2,),
            )
        )
        return out

    def _deliverables(self) -> list[Flowable]:
        out = self._heading("Agent Deliverables")
        for stage in self.view.stages:
            out += self._stage(stage)
        return out

    def _stage(self, stage: StageView) -> list[Flowable]:
        heading = self._p(f"{self.section}.{stage.order} {stage.agent} — {stage.produces}", "h2")
        tone = STATUS_TONE.get(stage.status, "neutral")
        if not stage.attempts:
            return [KeepTogether([heading, self._p("No output was produced.", "muted")])]
        latest = stage.attempts[-1]
        facts = (
            f"{len(stage.attempts)} attempt(s) · final model {latest.model or '—'} "
            f"({latest.tier or '—'}) · {_num(latest.input_tokens)} in / "
            f"{_num(latest.output_tokens)} out tokens · {_usd(latest.cost_usd)}"
        )
        status = self._p(
            f'<font color="#{TONES[tone][0].hexval()[2:]}"><b>'
            f"{escape(_status_label(stage.status))}</b></font>"
            f"{'' if stage.latest_score is None else f' · score {stage.latest_score:.2f}'}"
            f" · {escape(self.fonts.clean(facts))}",
            "muted",
            markup=True,
        )
        out: list[Flowable] = [KeepTogether([heading, status, Spacer(1, 1.5 * mm)])]
        if latest.output is None:
            out.append(self._p(f"No valid output: {latest.parse_error or 'pending'}", "muted"))
        else:
            out += ARTIFACT_RENDERERS.get(stage.persona, _Report._generic)(self, latest.output)
        out += self._retry_history(stage.attempts)
        if latest.evaluation is not None:
            out += [Spacer(1, 2 * mm), self._evaluation_box(latest.evaluation)]
        return out

    def _retry_history(self, attempts: Sequence[AttemptView]) -> list[Flowable]:
        earlier = [a for a in attempts[:-1] if a.evaluation is not None]
        if not earlier:
            return []
        items = []
        for a in earlier:
            e = a.evaluation
            assert e is not None
            issues = "; ".join(i.rstrip(". ") for i in e.blocking_issues)
            issues = f" Blocking issues: {issues}." if issues else ""
            items.append(
                f"Attempt {a.attempt}: {e.verdict.upper()} at {_score(e.overall_score)}"
                f"{' (critical)' if e.critical_flag else ''}. {e.feedback}{issues}"
            )
        return self._labelled_bullets("Retry history", items)

    def _evaluation_box(self, e: EvaluationView) -> Table:
        lines: list[Flowable] = [
            self._p(
                f"Evaluator verdict at {e.handoff_label}: {e.verdict.upper()} · score "
                f"{_score(e.overall_score)} · {e.judge} judge ({e.model_used or 'rules'})",
                "cell_b",
            ),
            self._p(e.feedback, "cell"),
        ]
        lines += [self._p(f"Blocking: {issue}", "cell") for issue in e.blocking_issues]
        return self._callout(lines, _verdict_tone(e.verdict))

    # ------------------------------------------------------------------ artifacts
    def _text_block(self, label: str, value: Any) -> list[Flowable]:
        return [self._p(label, "h3"), self._p(str(value))] if value else []

    def _pm(self, o: dict[str, Any]) -> list[Flowable]:
        out = self._text_block("Objective", o.get("objective"))
        for key in (
            "business_goals",
            "success_metrics",
            "constraints",
            "out_of_scope",
            "stakeholders",
        ):
            out += self._labelled_bullets(_title(key), [str(x) for x in o.get(key, [])])
        return out

    def _ba(self, o: dict[str, Any]) -> list[Flowable]:
        out: list[Flowable] = []
        stories = o.get("user_stories", [])
        if stories:
            out.append(self._p("User stories", "h3"))
            rows = []
            for s in stories:
                story = self._p(
                    f"<b>As a</b> {escape(self.fonts.clean(str(s.get('as_a', ''))))}, "
                    f"<b>I want</b> {escape(self.fonts.clean(str(s.get('i_want', ''))))}, "
                    f"<b>so that</b> {escape(self.fonts.clean(str(s.get('so_that', ''))))}.",
                    "cell",
                    markup=True,
                )
                criteria = [
                    self._p(
                        f"<b>{escape(str(c.get('id', '')))}</b> "
                        f"{escape(self.fonts.clean(str(c.get('description', ''))))}",
                        "cell",
                        markup=True,
                    )
                    for c in s.get("acceptance_criteria", [])
                ]
                rows.append([self._p(str(s.get("id", "")), "cell_b"), story, criteria or "—"])
            out.append(
                self._table(["ID", "Story", "Acceptance criteria"], rows, (0.09, 0.36, 0.55))
            )
        for key in ("non_functional_requirements", "open_questions"):
            out += self._labelled_bullets(_title(key), [str(x) for x in o.get(key, [])])
        return out

    def _architect(self, o: dict[str, Any]) -> list[Flowable]:
        out: list[Flowable] = []
        components = o.get("components", [])
        if components:
            out.append(self._p("Components", "h3"))
            rows = [
                [self._p(str(c.get("name", "")), "cell_b"), str(c.get("responsibility", ""))]
                for c in components
            ]
            out.append(self._table(["Component", "Responsibility"], rows, (0.28, 0.72)))
        out += self._text_block("Integration approach", o.get("integration_approach"))
        out += self._text_block("Data flow", o.get("data_flow"))
        risks = o.get("risks", [])
        if risks:
            out.append(self._p("Risks and mitigations", "h3"))
            rows = [[str(r.get("description", "")), str(r.get("mitigation", ""))] for r in risks]
            out.append(self._table(["Risk", "Mitigation"], rows, (0.5, 0.5)))
        for adr in o.get("adrs", []):
            alternatives = "; ".join(str(a) for a in adr.get("alternatives_considered", []))
            out.append(
                KeepTogether(
                    [
                        self._p(f"ADR: {adr.get('title', '')}", "h3"),
                        self._lead("Decision.", str(adr.get("decision", ""))),
                        self._lead("Alternatives.", alternatives or "—"),
                        self._lead("Rationale.", str(adr.get("rationale", ""))),
                    ]
                )
            )
        return out

    def _dev(self, o: dict[str, Any]) -> list[Flowable]:
        out = self._text_block("Summary", o.get("summary"))
        implemented = ", ".join(str(s) for s in o.get("implemented_story_ids", []))
        out += self._text_block("Implemented stories", implemented)
        out += self._labelled_bullets(
            "Stubbed stories",
            [f"{s.get('story_id', '')}: {s.get('plan', '')}" for s in o.get("stubbed_stories", [])],
        )
        out += self._labelled_bullets("Deviations", [str(x) for x in o.get("deviations", [])])
        files = o.get("files", [])
        if files:
            out.append(self._p("Source files", "h3"))
        for file in files:
            content = str(file.get("content", "")).rstrip() or "(empty file)"
            out.append(self._p(f"{file.get('path', '')}  ·  {file.get('language', '')}", "path"))
            out.append(self._code(content))
        return out

    def _code(self, content: str, lines_per_row: int = 30) -> Table:
        """Shaded code block; split into rows so long files flow across pages."""
        lines = self.fonts.clean(content.expandtabs(4)).splitlines()
        chunks = [
            "\n".join(lines[i : i + lines_per_row]) for i in range(0, len(lines), lines_per_row)
        ]
        rows = [
            [Preformatted(c, self.styles["code"], maxLineLength=108, newLineChars="  ")]
            for c in chunks
        ]
        block = Table(rows, colWidths=[CONTENT_W])
        block.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), ZEBRA),
                    ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                    ("LINEBEFORE", (0, 0), (0, -1), 2, ACCENT),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 1),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                    ("TOPPADDING", (0, 0), (-1, 0), 5),
                    ("BOTTOMPADDING", (0, -1), (-1, -1), 5),
                ]
            )
        )
        return block

    def _qa(self, o: dict[str, Any]) -> list[Flowable]:
        out: list[Flowable] = []
        cases = o.get("test_cases", [])
        if cases:
            out.append(self._p("Test cases", "h3"))
            rows = [
                [
                    self._p(str(c.get("id", "")), "cell_b"),
                    str(c.get("story_id", "")),
                    ", ".join(str(x) for x in c.get("acceptance_criterion_ids", [])),
                    str(c.get("type", "")),
                    [
                        self._p(f"{i}. {step}", "cell")
                        for i, step in enumerate(c.get("steps", []), start=1)
                    ]
                    or "—",
                    str(c.get("expected_result", "")),
                ]
                for c in cases
            ]
            out.append(
                self._table(
                    ["ID", "Story", "Criteria", "Type", "Steps", "Expected result"],
                    rows,
                    (0.07, 0.08, 0.13, 0.09, 0.35, 0.28),
                )
            )
        out += self._text_block("Coverage summary", o.get("coverage_summary"))
        out += self._labelled_bullets("Gaps", [str(x) for x in o.get("gaps", [])])
        untestable = ", ".join(str(x) for x in o.get("untestable_criteria", []))
        out += self._text_block("Criteria not testable yet", untestable)
        return out

    def _generic(self, o: dict[str, Any]) -> list[Flowable]:
        out: list[Flowable] = []
        for key, value in o.items():
            if isinstance(value, list) and all(isinstance(x, str) for x in value):
                out += self._labelled_bullets(_title(key), value)
            elif isinstance(value, str | int | float):
                out += self._text_block(_title(key), value)
            elif value:
                out += self._text_block(_title(key), _flatten(value))
        return out

    # ------------------------------------------------------------------ cost & timeline
    def _costs(self) -> list[Flowable]:
        v = self.view
        out = self._heading("Cost & Performance")
        rows: list[list[object]] = []
        for s in v.stages:
            models = sorted({a.model for a in s.attempts if a.model})
            rows.append(
                [
                    s.agent,
                    str(len(s.attempts)),
                    ", ".join(models) or "—",
                    _num(sum(a.input_tokens for a in s.attempts)),
                    _num(sum(a.output_tokens for a in s.attempts)),
                    _usd(sum(a.cost_usd for a in s.attempts)),
                    _duration(s.started_at, s.finished_at),
                ]
            )
        ev, t = v.evaluator, v.totals
        rows.append(
            [
                ev.agent,
                str(ev.judge_calls),
                "judge",
                _num(ev.input_tokens),
                _num(ev.output_tokens),
                _usd(ev.cost_usd),
                "—",
            ]
        )
        rows.append(
            [
                self._p("Total", "cell_b"),
                str(t.llm_calls),
                "",
                _num(t.input_tokens),
                _num(t.output_tokens),
                self._p(_usd(t.cost_usd), "cell_b"),
                _duration(v.started_at, v.finished_at),
            ]
        )
        last = len(rows)
        out.append(
            self._table(
                ["Agent", "Calls", "Model(s)", "Tokens in", "Tokens out", "Cost", "Duration"],
                rows,
                (0.2, 0.07, 0.27, 0.12, 0.12, 0.11, 0.11),
                center=(1, 3, 4, 5, 6),
                extra=[
                    ("BACKGROUND", (0, last), (-1, last), SHADE),
                    ("LINEABOVE", (0, last), (-1, last), 0.8, NAVY),
                ],
            )
        )
        cache = ""
        if t.cache_read_tokens or t.cache_write_tokens:
            cache = (
                f" Prompt cache: {_num(t.cache_read_tokens)} tokens read, "
                f"{_num(t.cache_write_tokens)} written."
            )
        out += [
            Spacer(1, 2 * mm),
            self._p(
                f"Model latency {t.llm_latency_ms / 1000:.1f} s across {t.llm_calls} call(s); "
                f"{t.retries} retry(ies), {t.escalations} escalation(s), {t.human_decisions} "
                f"human decision(s).{cache}",
                "muted",
            ),
        ]
        return out

    def _timeline(self) -> list[Flowable]:
        entries = self.view.timeline
        if not entries:
            return []
        out = self._heading("Execution Timeline")
        rows = [
            [
                _when(e.ts, time_only=True),
                self._p(e.actor, "cell_b"),
                self._tone_text(e.message, e.tone, "cell") if e.tone != "info" else e.message,
            ]
            for e in entries
        ]
        out.append(self._table(["Time (UTC)", "Actor", "Event"], rows, (0.12, 0.22, 0.66)))
        return out


def _verdict_tone(verdict: str) -> str:
    return {"pass": "success", "retry": "warning", "escalate": "danger"}.get(verdict, "neutral")


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(f"{_title(str(k))}: {_flatten(v)}" for k, v in value.items())
    if isinstance(value, list):
        return "; ".join(_flatten(v) for v in value)
    return str(value)


ARTIFACT_RENDERERS: dict[str, Callable[[_Report, dict[str, Any]], list[Flowable]]] = {
    "pm": _Report._pm,
    "ba": _Report._ba,
    "architect": _Report._architect,
    "dev": _Report._dev,
    "qa": _Report._qa,
}
