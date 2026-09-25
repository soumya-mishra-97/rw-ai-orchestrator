"""PII redaction applied to the business request *before* it reaches any model
or log, and again (defence in depth) to every payload written to the audit log.

``RegexRedactor`` is dependency-free and covers the high-risk structured
identifiers (emails, card numbers with a Luhn check, SSNs, IBANs, phone numbers,
IPs). ``PresidioRedactor`` adds NER-based detection of names/locations when the
optional ``pii`` extra is installed — that is what production would run.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def redacted(self) -> bool:
        return bool(self.counts)


class Redactor(Protocol):
    def redact(self, text: str) -> RedactionResult: ...


def _luhn_ok(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, digit in enumerate(reversed(digits)):
        doubled = digit * 2 if i % 2 == 1 else digit
        total += doubled - 9 if doubled > 9 else doubled
    return total % 10 == 0


_Validator = Callable[[str], bool]

_PATTERNS: tuple[tuple[str, re.Pattern[str], _Validator | None], ...] = (
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), None),
    ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), _luhn_ok),
    ("US_SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), None),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b"), None),
    ("IP_ADDRESS", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), None),
    (
        "PHONE",
        # (?<![\w-]) / (?![\w-]): digits glued to an identifier by a hyphen
        # (model ids like claude-haiku-4-5-20251001, run ids) are not phone numbers.
        re.compile(r"(?<![\w-])\+?\d{1,3}[ .-]?\(?\d{2,4}\)?[ .-]?\d{3,4}[ .-]?\d{3,4}(?![\w-])"),
        None,
    ),
)


class RegexRedactor:
    def redact(self, text: str) -> RedactionResult:
        counts: Counter[str] = Counter()
        seen: dict[str, str] = {}

        def _sub(entity: str, validator: _Validator | None) -> Callable[[re.Match[str]], str]:
            def repl(match: re.Match[str]) -> str:
                value = match.group(0)
                if validator is not None and not validator(value):
                    return value
                if value not in seen:
                    counts[entity] += 1
                    seen[value] = f"<{entity}_{counts[entity]}>"
                return seen[value]

            return repl

        for entity, pattern, validator in _PATTERNS:
            text = pattern.sub(_sub(entity, validator), text)
        return RedactionResult(text=text, counts=dict(counts))


class PresidioRedactor:  # pragma: no cover - optional dependency
    def __init__(self) -> None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_anonymizer import AnonymizerEngine

        self._analyzer = AnalyzerEngine()
        self._anonymizer = AnonymizerEngine()
        self._fallback = RegexRedactor()

    def redact(self, text: str) -> RedactionResult:
        results = self._analyzer.analyze(text=text, language="en")
        counts = Counter(r.entity_type for r in results)
        anonymized = self._anonymizer.anonymize(text=text, analyzer_results=results).text
        # Regex pass catches anything the NER model missed (e.g. Luhn-valid cards).
        second = self._fallback.redact(anonymized)
        counts.update(second.counts)
        return RedactionResult(text=second.text, counts=dict(counts))


def build_redactor(backend: str) -> Redactor:
    if backend == "presidio":
        return PresidioRedactor()
    return RegexRedactor()


def redact_structure(value: Any, redactor: Redactor) -> Any:
    """Recursively redact every string inside a JSON-like structure."""
    if isinstance(value, str):
        return redactor.redact(value).text
    if isinstance(value, dict):
        return {k: redact_structure(v, redactor) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_structure(v, redactor) for v in value]
    return value
