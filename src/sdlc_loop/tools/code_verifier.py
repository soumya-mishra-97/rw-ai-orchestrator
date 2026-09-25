"""Runs what the Dev agent wrote, so QA reports *measured* results.

Always: materialise files in a temp dir (path-traversal safe) and syntax-check
every Python file — no code is executed for this.

Opt-in (``SDLC_EXECUTE_GENERATED_CODE=true``): run the generated ``test_*.py``
files with pytest in a subprocess with a timeout, a scrubbed environment and a
throwaway working directory. That is a *minimal* sandbox suitable for a local
exercise; production must run this in an isolated container with no network and
no credentials (see docs/governance.md).
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

from sdlc_loop.quality.checks import python_syntax_errors
from sdlc_loop.schemas.artifacts import ExecutionReport, Implementation

_SUMMARY = re.compile(r"(?P<n>\d+) (?P<kind>passed|failed|error|errors)")


class UnsafePathError(ValueError):
    pass


def _safe_relative(path: str) -> PurePosixPath:
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise UnsafePathError(f"refusing to write generated file outside sandbox: {path!r}")
    return p


class CodeVerifier:
    def __init__(self, *, execute: bool, timeout_s: float = 30.0) -> None:
        self._execute = execute
        self._timeout_s = timeout_s

    def verify(self, impl: Implementation) -> ExecutionReport:  # noqa: PLR0911 - guard clauses
        syntax_errors = python_syntax_errors(impl)
        py_files = [f for f in impl.files if f.path.endswith(".py")]
        test_files = [f for f in py_files if PurePosixPath(f.path).name.startswith("test_")]

        def report(
            *, executed: bool, passed: int = 0, failed: int = 0, reason: str | None, tail: str = ""
        ) -> ExecutionReport:
            return ExecutionReport(
                files_checked=len(py_files),
                syntax_errors=syntax_errors,
                tests_executed=executed,
                tests_passed=passed,
                tests_failed=failed,
                skipped_reason=reason,
                output_tail=tail[-2000:],
            )

        if syntax_errors:
            return report(executed=False, reason="syntax errors — tests not executed")
        if not test_files:
            return report(executed=False, reason="implementation contains no test_*.py files")
        if not self._execute:
            return report(
                executed=False,
                reason="execution disabled (set SDLC_EXECUTE_GENERATED_CODE=true to run tests)",
            )
        if importlib.util.find_spec("pytest") is None:
            return report(executed=False, reason="pytest not installed in runtime")

        with tempfile.TemporaryDirectory(prefix="sdlc-verify-") as tmp:
            root = Path(tmp)
            try:
                for f in impl.files:
                    target = root / _safe_relative(f.path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(f.content, encoding="utf-8")
            except UnsafePathError as exc:
                return report(executed=False, reason=str(exc))
            env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
            cmd = [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
            try:
                proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    [*cmd, "--rootdir", str(root)],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return report(executed=True, reason=f"timed out after {self._timeout_s}s")
            output = proc.stdout + proc.stderr
            lines = [line for line in output.splitlines() if line.strip()]
            summary_line = lines[-1] if lines else ""  # pytest's "N failed, M passed in Xs"
            counts = {"passed": 0, "failed": 0}
            for m in _SUMMARY.finditer(summary_line):
                kind = "passed" if m["kind"] == "passed" else "failed"
                counts[kind] += int(m["n"])
            return report(
                executed=True,
                passed=counts["passed"],
                failed=counts["failed"],
                reason=None,
                tail=output,
            )
