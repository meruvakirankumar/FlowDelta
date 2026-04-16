"""
Mutation Testing Feedback Loop – Sprint 3 of FlowDelta.

Drives ``mutmut`` (or a subprocess fallback) against the generated test
suite and collects a *mutation score* — the fraction of injected bugs
that the tests caught.

A score < threshold triggers a feedback loop:
  1. Identify surviving mutants (bugs the tests missed)
  2. Map each mutant back to the FlowDelta variable / assertion that
     should have caught it
  3. Suggest strengthened assertions or new test cases

The loop runs up to ``max_rounds`` iterations, each time regenerating
assertions with tighter constraints based on surviving mutant analysis.

Usage::

    runner = MutationRunner(
        source_file="examples/sample_app/ecommerce.py",
        test_file="generated_tests/test_checkout.py",
    )
    report = runner.run()
    print(report.summary())

    if report.score < 0.8:
        suggestions = runner.suggest_improvements(report)
        for s in suggestions:
            print(s)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MutantResult:
    """Result for one mutant."""
    mutant_id: str
    status: str          # "killed" | "survived" | "timeout" | "suspicious"
    description: str     # e.g. "Replace + with -"
    file: str
    line: int
    original: str
    mutated: str


@dataclass
class MutationReport:
    """
    Full mutation testing report.

    Attributes
    ----------
    source_file : str
        File under mutation.
    test_file : str
        Test file exercising the source.
    total : int
        Total mutants generated.
    killed : int
        Mutants caught by the tests.
    survived : int
        Mutants that were NOT caught.
    score : float
        ``killed / total`` (mutation score, 0.0–1.0).
    mutants : list[MutantResult]
        Individual mutant results.
    """
    source_file: str
    test_file: str
    total: int = 0
    killed: int = 0
    survived: int = 0
    timeout: int = 0
    score: float = 0.0
    mutants: List[MutantResult] = field(default_factory=list)
    raw_output: str = ""

    def summary(self) -> str:
        lines = [
            f"Mutation Testing Report",
            f"  Source : {self.source_file}",
            f"  Tests  : {self.test_file}",
            f"  Total  : {self.total}",
            f"  Killed : {self.killed}",
            f"  Survived: {self.survived}",
            f"  Timeouts: {self.timeout}",
            f"  Score  : {self.score:.1%}",
        ]
        if self.survived_mutants:
            lines.append("\nSurviving mutants (tests did not catch):")
            for m in self.survived_mutants[:10]:
                lines.append(f"  Line {m.line}: {m.description}")
                lines.append(f"    Original: {m.original.strip()}")
                lines.append(f"    Mutated:  {m.mutated.strip()}")
        return "\n".join(lines)

    @property
    def survived_mutants(self) -> List[MutantResult]:
        return [m for m in self.mutants if m.status == "survived"]

    def to_dict(self) -> dict:
        return {
            "source_file": self.source_file,
            "test_file": self.test_file,
            "total": self.total,
            "killed": self.killed,
            "survived": self.survived,
            "timeout": self.timeout,
            "score": round(self.score, 4),
            "survived_mutants": [
                {
                    "mutant_id": m.mutant_id,
                    "description": m.description,
                    "file": m.file,
                    "line": m.line,
                    "original": m.original,
                    "mutated": m.mutated,
                }
                for m in self.survived_mutants
            ],
        }


@dataclass
class ImprovementSuggestion:
    """A suggestion for strengthening the test suite."""
    line: int
    original: str
    mutated: str
    description: str
    suggested_assertion: str
    rationale: str

    def __str__(self) -> str:
        return (
            f"Line {self.line}: {self.description}\n"
            f"  Mutant: {self.mutated.strip()}\n"
            f"  Add:    {self.suggested_assertion}\n"
            f"  Why:    {self.rationale}"
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class MutationRunner:
    """
    Drives mutation testing against a FlowDelta-generated test file.

    Supports two backends:
    * **mutmut** — ``pip install mutmut``, battle-tested Python mutation tool
    * **builtin** — lightweight operator-substitution fallback (no extra deps)

    Parameters
    ----------
    source_file : str | Path
        The production code file to mutate.
    test_file : str | Path
        The pytest file to run against each mutant.
    backend : str
        ``"mutmut"`` (recommended) or ``"builtin"``.
    threshold : float
        Minimum acceptable mutation score (0.0–1.0).
    max_rounds : int
        Maximum feedback loop iterations.
    timeout_seconds : int
        Per-mutant test run timeout.
    """

    _OPERATORS = [
        # (pattern, replacement, description)
        (r"\+", "-", "Replace + with -"),
        (r"\-", "+", "Replace - with +"),
        (r"\*", "/", "Replace * with /"),
        (r"==", "!=", "Replace == with !="),
        (r"!=", "==", "Replace != with =="),
        (r"<=", "<",  "Replace <= with <"),
        (r">=", ">",  "Replace >= with >"),
        (r"\bTrue\b",  "False", "Replace True with False"),
        (r"\bFalse\b", "True",  "Replace False with True"),
        (r"\band\b",   "or",    "Replace and with or"),
        (r"\bor\b",    "and",   "Replace or with and"),
        (r"\breturn\b\s+None", "return ''", "Replace return None with return ''"),
    ]

    def __init__(
        self,
        source_file: str | Path,
        test_file: str | Path,
        backend: str = "builtin",
        threshold: float = 0.8,
        max_rounds: int = 3,
        timeout_seconds: int = 30,
    ) -> None:
        self.source_file = Path(source_file)
        self.test_file = Path(test_file)
        self.backend = backend
        self.threshold = threshold
        self.max_rounds = max_rounds
        self.timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> MutationReport:
        """
        Execute mutation testing and return a :class:`MutationReport`.
        Uses ``mutmut`` if available and configured, otherwise the builtin runner.
        """
        if self.backend == "mutmut" and self._mutmut_available():
            return self._run_mutmut()
        return self._run_builtin()

    def suggest_improvements(self, report: MutationReport) -> List[ImprovementSuggestion]:
        """
        For each surviving mutant, suggest an assertion that would kill it.
        """
        suggestions: List[ImprovementSuggestion] = []
        for mutant in report.survived_mutants:
            s = self._suggest_for_mutant(mutant)
            if s:
                suggestions.append(s)
        return suggestions

    def feedback_loop(self) -> Tuple[MutationReport, List[ImprovementSuggestion]]:
        """
        Run mutation testing, check score, and return report + suggestions.
        If score < threshold, returns improvement suggestions.
        """
        report = self.run()
        suggestions = []
        if report.score < self.threshold:
            suggestions = self.suggest_improvements(report)
        return report, suggestions

    # ------------------------------------------------------------------
    # mutmut backend
    # ------------------------------------------------------------------

    def _mutmut_available(self) -> bool:
        try:
            result = subprocess.run(
                ["mutmut", "--version"],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _run_mutmut(self) -> MutationReport:
        """Run mutmut and parse its output."""
        cmd = [
            "mutmut", "run",
            "--paths-to-mutate", str(self.source_file),
            "--tests-dir", str(self.test_file.parent),
            "--runner", f"python -m pytest {self.test_file} -x -q",
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds * 100,
        )
        raw = result.stdout + result.stderr
        return self._parse_mutmut_output(raw)

    def _parse_mutmut_output(self, raw: str) -> MutationReport:
        """Parse mutmut text output into a MutationReport."""
        report = MutationReport(
            source_file=str(self.source_file),
            test_file=str(self.test_file),
            raw_output=raw,
        )
        # mutmut summary: "Killed X out of Y mutants"
        m = re.search(r"Killed\s+(\d+)\s+out\s+of\s+(\d+)", raw)
        if m:
            report.killed = int(m.group(1))
            report.total = int(m.group(2))
            report.survived = report.total - report.killed
            report.score = report.killed / report.total if report.total else 0.0
        return report

    # ------------------------------------------------------------------
    # Builtin backend
    # ------------------------------------------------------------------

    def _run_builtin(self) -> MutationReport:
        """
        Lightweight mutation runner using simple text substitutions.
        Reads the source file, applies each operator mutation line-by-line,
        runs pytest on the mutant, and records whether the tests killed it.
        """
        source_lines = self.source_file.read_text(encoding="utf-8").splitlines()
        report = MutationReport(
            source_file=str(self.source_file),
            test_file=str(self.test_file),
        )

        mutant_id = 0
        for line_idx, original_line in enumerate(source_lines):
            line_no = line_idx + 1
            # Skip comments and blank lines
            stripped = original_line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            for pattern, replacement, description in self._OPERATORS:
                mutated_line, count = re.subn(pattern, replacement, original_line, count=1)
                if count == 0 or mutated_line == original_line:
                    continue

                mutant_id += 1
                mutant_lines = source_lines.copy()
                mutant_lines[line_idx] = mutated_line

                status = self._test_mutant(
                    mutant_source="\n".join(mutant_lines),
                    mutant_id=mutant_id,
                )
                report.mutants.append(MutantResult(
                    mutant_id=str(mutant_id),
                    status=status,
                    description=description,
                    file=str(self.source_file),
                    line=line_no,
                    original=original_line,
                    mutated=mutated_line,
                ))

        report.total = len(report.mutants)
        report.killed = sum(1 for m in report.mutants if m.status == "killed")
        report.survived = sum(1 for m in report.mutants if m.status == "survived")
        report.timeout = sum(1 for m in report.mutants if m.status == "timeout")
        report.score = report.killed / report.total if report.total else 0.0
        return report

    def _test_mutant(self, mutant_source: str, mutant_id: int) -> str:
        """
        Write the mutant to a temp file, run pytest, return status.
        Returns ``"killed"`` if tests fail (good), ``"survived"`` if they pass (bad).
        """
        import tempfile

        # Write mutant to a sidecar file next to the original
        mutant_path = self.source_file.parent / f"_mutant_{mutant_id}_{self.source_file.name}"
        try:
            mutant_path.write_text(mutant_source, encoding="utf-8")

            # Patch sys.path so the test can import the mutant
            env = os.environ.copy()
            env["FLOWDELTA_MUTANT"] = str(mutant_path)

            cmd = [
                sys.executable, "-m", "pytest",
                str(self.test_file),
                "-x", "-q", "--tb=no", "--no-header",
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self.timeout_seconds,
                env=env,
            )
            # pytest exits 1 when tests fail → mutant killed
            return "killed" if result.returncode != 0 else "survived"
        except subprocess.TimeoutExpired:
            return "timeout"
        finally:
            if mutant_path.exists():
                mutant_path.unlink()

    # ------------------------------------------------------------------
    # Suggestion generator
    # ------------------------------------------------------------------

    def _suggest_for_mutant(self, mutant: MutantResult) -> Optional[ImprovementSuggestion]:
        """Map a surviving mutant to an assertion suggestion."""
        desc = mutant.description

        if "==" in desc and "!=" in desc.replace("==", ""):
            suggestion = f"assert result == expected_value  # catches {desc}"
            rationale = "Equality check was not strict enough to catch this mutation."
        elif "+" in desc or "-" in desc:
            suggestion = f"assert result > 0  # or assert result == exact_expected"
            rationale = "Arithmetic mutation survived; add a precise value assertion."
        elif "True" in desc or "False" in desc:
            suggestion = f"assert bool_var == True  # explicit boolean check"
            rationale = "Boolean mutation was not caught; assert the exact boolean value."
        elif "and" in desc or "or" in desc:
            suggestion = f"# Add a test case exercising the False branch of the condition"
            rationale = "Logic operator mutation survived; add a boundary test case."
        elif "<=" in desc or ">=" in desc:
            suggestion = f"assert value == boundary_value  # test the boundary exactly"
            rationale = "Boundary comparison mutation survived; add an equality test at the boundary."
        else:
            suggestion = f"# Strengthen assertion at line {mutant.line}"
            rationale = f"Mutation '{desc}' survived; review assertions near this line."

        return ImprovementSuggestion(
            line=mutant.line,
            original=mutant.original,
            mutated=mutant.mutated,
            description=desc,
            suggested_assertion=suggestion,
            rationale=rationale,
        )
