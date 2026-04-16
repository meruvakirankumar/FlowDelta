"""
Invariant Detector – Sprint 3 of FlowDelta.

Analyses a :class:`FlowTrace` (or multiple traces) to discover variables
that remain *stable* across all observed state transitions:

* **Never-change invariants** — variable appears in every snapshot and
  its value is constant throughout the entire trace.
* **Monotonic invariants** — numeric variable that is always increasing
  or always decreasing (never reverses direction).
* **Non-null invariants** — variable is present in every snapshot and
  is never ``None`` or empty.
* **Type invariants** — variable always has the same Python type.

Invariants are surfaced as high-priority ``Assertion`` objects that can
be injected into the test spec produced by :class:`AssertionGenerator`.
They catch regressions where a previously stable variable unexpectedly
changes — a class of bug that delta-only assertions miss.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..state_tracker.trace_recorder import FlowTrace
from ..state_tracker.dap_client import StateSnapshot
from .assertion_gen import Assertion


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Invariant:
    """
    A detected invariant: a property that held across all observed snapshots.

    Attributes
    ----------
    variable : str
        Top-level variable name.
    kind : str
        ``never_changes`` | ``monotonic_increase`` | ``monotonic_decrease`` |
        ``never_null`` | ``stable_type``
    observed_value : Any
        The constant value (for ``never_changes``) or representative sample.
    observed_type : str
        The stable Python type name.
    snapshot_count : int
        How many snapshots the variable appeared in.
    confidence : float
        0.0–1.0. Higher = more snapshots observed.
    """
    variable: str
    kind: str
    observed_value: Any = None
    observed_type: str = ""
    snapshot_count: int = 0
    confidence: float = 1.0

    def to_assertion(self, result_expr: str = "result") -> Assertion:
        """Convert this invariant into a pytest :class:`Assertion`."""
        var_expr = f"{result_expr}[{self.variable!r}]"

        if self.kind == "never_changes":
            code = f"assert {var_expr} == {self.observed_value!r}  # invariant"
            desc = f"{self.variable} never changes (invariant: always {self.observed_value!r})"
        elif self.kind == "monotonic_increase":
            code = f"assert {var_expr} >= {self.observed_value!r}  # monotonic ↑ invariant"
            desc = f"{self.variable} monotonically increases (invariant)"
        elif self.kind == "monotonic_decrease":
            code = f"assert {var_expr} <= {self.observed_value!r}  # monotonic ↓ invariant"
            desc = f"{self.variable} monotonically decreases (invariant)"
        elif self.kind == "never_null":
            code = f"assert {var_expr} is not None  # invariant"
            desc = f"{self.variable} is never None (invariant)"
        elif self.kind == "stable_type":
            code = f"assert isinstance({var_expr}, {self.observed_type})  # invariant"
            desc = f"{self.variable} always has type {self.observed_type} (invariant)"
        else:
            code = f"assert {var_expr} is not None  # invariant"
            desc = f"{self.variable} invariant holds"

        return Assertion(
            code=code,
            description=desc,
            priority=1,   # invariants are highest priority
            variable=self.variable,
            change_type="invariant",
            location="(invariant across all snapshots)",
        )

    def to_dict(self) -> dict:
        return {
            "variable": self.variable,
            "kind": self.kind,
            "observed_value": _safe_json(self.observed_value),
            "observed_type": self.observed_type,
            "snapshot_count": self.snapshot_count,
            "confidence": round(self.confidence, 4),
        }


def _safe_json(v: Any) -> Any:
    """Return a JSON-safe representation of *v*."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return repr(v)


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class InvariantDetector:
    """
    Detect stable properties across all snapshots in one or more traces.

    Parameters
    ----------
    min_snapshots : int
        Minimum number of snapshots a variable must appear in to be considered.
    min_confidence : float
        Minimum ratio of snapshots where the property holds (0.0–1.0).
    numeric_tolerance : float
        Relative tolerance when comparing numeric values for equality.
    """

    def __init__(
        self,
        min_snapshots: int = 2,
        min_confidence: float = 1.0,
        numeric_tolerance: float = 1e-9,
    ) -> None:
        self.min_snapshots = min_snapshots
        self.min_confidence = min_confidence
        self.numeric_tolerance = numeric_tolerance

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(self, trace: FlowTrace) -> List[Invariant]:
        """
        Analyse *trace* and return all detected invariants.

        Parameters
        ----------
        trace : FlowTrace
            A recorded execution trace.

        Returns
        -------
        list[Invariant]
            Invariants sorted by confidence (desc), then variable name.
        """
        return self.detect_multi([trace])

    def detect_multi(self, traces: Sequence[FlowTrace]) -> List[Invariant]:
        """
        Detect invariants across multiple traces of the same flow.
        More traces → higher confidence.
        """
        # Collect all (variable, value) observations per snapshot
        # structure: {var_name: [value, value, ...]}
        observations: Dict[str, List[Any]] = defaultdict(list)
        types_seen: Dict[str, set] = defaultdict(set)

        for trace in traces:
            for snap in trace.snapshots:
                locals_ = snap.locals
                if not isinstance(locals_, dict):
                    continue
                for var, val in locals_.items():
                    if var.startswith("_"):
                        continue
                    observations[var].append(val)
                    types_seen[var].add(type(val).__name__)

        invariants: List[Invariant] = []

        for var, values in observations.items():
            if len(values) < self.min_snapshots:
                continue

            never_change = self._check_never_changes(var, values)
            if never_change:
                invariants.append(never_change)
                # still check never_null (value might be a constant non-null)
                inv = self._check_never_null(var, values)
                if inv:
                    invariants.append(inv)
                continue   # skip monotonic/stable_type for constants

            inv = self._check_monotonic(var, values)
            if inv:
                invariants.append(inv)
                # monotonic numerics also get stable_type — skip to avoid dup
                continue

            inv = self._check_never_null(var, values)
            if inv:
                invariants.append(inv)

            inv = self._check_stable_type(var, values, types_seen[var])
            if inv:
                invariants.append(inv)

        # Filter by min_confidence and sort
        result = [i for i in invariants if i.confidence >= self.min_confidence]
        result.sort(key=lambda i: (-i.confidence, i.variable))
        return result

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_never_changes(self, var: str, values: List[Any]) -> Optional[Invariant]:
        """Variable holds the same value in every snapshot."""
        if len(values) < 2:
            return None
        first = values[0]
        # All values must be JSON-primitive and equal to the first
        if not isinstance(first, (bool, int, float, str, type(None))):
            return None
        for v in values[1:]:
            if not _approximately_equal(first, v, self.numeric_tolerance):
                return None
        return Invariant(
            variable=var,
            kind="never_changes",
            observed_value=first,
            observed_type=type(first).__name__,
            snapshot_count=len(values),
            confidence=1.0,
        )

    def _check_never_null(self, var: str, values: List[Any]) -> Optional[Invariant]:
        """Variable is never None or empty string/list across all snapshots."""
        null_count = sum(
            1 for v in values
            if v is None or v == "" or v == [] or v == {}
        )
        if null_count > 0:
            return None
        return Invariant(
            variable=var,
            kind="never_null",
            observed_value=values[0],
            observed_type=type(values[0]).__name__,
            snapshot_count=len(values),
            confidence=1.0,
        )

    def _check_monotonic(self, var: str, values: List[Any]) -> Optional[Invariant]:
        """Numeric variable that strictly increases or decreases."""
        numerics = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if len(numerics) < 3:   # need at least 3 points to call monotonic
            return None
        if all(numerics[i] <= numerics[i + 1] for i in range(len(numerics) - 1)):
            return Invariant(
                variable=var,
                kind="monotonic_increase",
                observed_value=numerics[-1],   # last (largest) observed
                observed_type=type(numerics[0]).__name__,
                snapshot_count=len(numerics),
                confidence=1.0,
            )
        if all(numerics[i] >= numerics[i + 1] for i in range(len(numerics) - 1)):
            return Invariant(
                variable=var,
                kind="monotonic_decrease",
                observed_value=numerics[-1],   # last (smallest) observed
                observed_type=type(numerics[0]).__name__,
                snapshot_count=len(numerics),
                confidence=1.0,
            )
        return None

    def _check_stable_type(
        self,
        var: str,
        values: List[Any],
        types: set,
    ) -> Optional[Invariant]:
        """Variable always has the same Python type."""
        if len(types) != 1:
            return None
        type_name = next(iter(types))
        # Don't emit for NoneType — too generic
        if type_name == "NoneType":
            return None
        return Invariant(
            variable=var,
            kind="stable_type",
            observed_value=values[0],
            observed_type=type_name,
            snapshot_count=len(values),
            confidence=1.0,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _approximately_equal(a: Any, b: Any, tol: float) -> bool:
    """Numeric-aware equality comparison."""
    if type(a) != type(b):
        return False
    if isinstance(a, float):
        if a == b:
            return True
        denom = max(abs(a), abs(b), 1e-300)
        return abs(a - b) / denom < tol
    return a == b
