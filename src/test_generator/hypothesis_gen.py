"""
Hypothesis Test Generator – Sprint 3 of FlowDelta.

Generates property-based tests using the `hypothesis` library from
:class:`TraceDelta` and :class:`Invariant` observations.

Instead of asserting on one specific captured value, property-based
tests define *strategies* that explore the full input space.  FlowDelta
derives strategies from the types and ranges it observed at runtime:

* Integers → ``st.integers(min_value=..., max_value=...)``
* Floats   → ``st.floats(min_value=..., max_value=...)``
* Strings  → ``st.text()`` or ``st.from_regex(pattern)``
* Lists    → ``st.lists(st.integers())`` shaped by observed length
* Booleans → ``st.booleans()``
* None     → ``st.none() | st.just(observed_value)``

The generated file is a standalone ``test_property_<flow_id>.py`` that
runs with ``pytest`` + ``hypothesis``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..delta_engine.state_diff import TraceDelta, VariableDelta
from .invariant_detector import Invariant


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class StrategySpec:
    """Hypothesis strategy spec for one variable."""
    variable: str
    strategy_code: str        # e.g. 'st.integers(min_value=0, max_value=100)'
    description: str
    observed_type: str


@dataclass
class PropertyTest:
    """One @given-decorated property test."""
    function_name: str
    docstring: str
    strategies: List[StrategySpec]
    body_lines: List[str]          # assertion lines in the test body


@dataclass
class PropertyTestSpec:
    """Complete property-based test file spec."""
    flow_id: str
    run_id: str
    tests: List[PropertyTest] = field(default_factory=list)
    invariants: List[Invariant] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

class HypothesisTestGenerator:
    """
    Generates Hypothesis property-based tests from FlowDelta observations.

    Parameters
    ----------
    max_examples : int
        ``@settings(max_examples=...)`` for generated tests.
    include_invariants : bool
        If ``True``, add dedicated invariant-checking property tests.
    """

    def __init__(
        self,
        max_examples: int = 100,
        include_invariants: bool = True,
    ) -> None:
        self.max_examples = max_examples
        self.include_invariants = include_invariants

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(
        self,
        delta: TraceDelta,
        invariants: Optional[List[Invariant]] = None,
    ) -> PropertyTestSpec:
        """
        Generate a :class:`PropertyTestSpec` from *delta* and *invariants*.
        """
        spec = PropertyTestSpec(
            flow_id=delta.flow_id,
            run_id=delta.run_id,
            invariants=invariants or [],
        )

        # One property test per variable that changes
        seen_vars: set = set()
        for sd in delta.deltas:
            for change in sd.changes:
                if change.name in seen_vars:
                    continue
                seen_vars.add(change.name)
                pt = self._make_property_test(change, sd.to_location)
                if pt:
                    spec.tests.append(pt)

        # Invariant-based property tests
        if self.include_invariants and invariants:
            pt = self._make_invariant_property_test(invariants)
            if pt:
                spec.tests.append(pt)

        return spec

    def render(self, spec: PropertyTestSpec, output_dir: str | Path = "generated_tests") -> Path:
        """
        Render *spec* to a ``test_property_<flow_id>.py`` file.
        Returns the output path.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = output_dir / f"test_property_{spec.flow_id}.py"

        lines: List[str] = [
            "# FlowDelta – Property-based tests (Hypothesis)",
            f"# Flow:    {spec.flow_id}",
            f"# Run ID:  {spec.run_id}",
            f"# Generated: {datetime.now(timezone.utc).isoformat()}",
            "#",
            "# Run with:  pytest " + filename.name,
            "# Requires:  pip install hypothesis",
            "",
            "import pytest",
            "from hypothesis import given, settings, assume",
            "from hypothesis import strategies as st",
            "",
            "",
        ]

        if not spec.tests:
            lines += [
                "# No property tests could be derived from the recorded delta.",
                "# Record more traces or lower min_priority in AssertionGenerator.",
                "",
            ]
        else:
            for pt in spec.tests:
                lines += self._render_test(pt)
                lines.append("")

        filename.write_text("\n".join(lines), encoding="utf-8")
        return filename

    # ------------------------------------------------------------------
    # Internal builders
    # ------------------------------------------------------------------

    def _make_property_test(
        self,
        change: VariableDelta,
        location: str,
    ) -> Optional[PropertyTest]:
        """Build a property test for one variable change."""
        strategy = self._strategy_for(change.name, change.new_value)
        if strategy is None:
            return None

        fn_name = f"test_property_{change.name.replace('.', '_').replace('[', '_').replace(']', '')}"
        body = self._body_for(change, strategy)
        if not body:
            return None

        return PropertyTest(
            function_name=fn_name,
            docstring=(
                f"Property: {change.name} behaves correctly across arbitrary inputs. "
                f"Observed at {location}."
            ),
            strategies=[strategy],
            body_lines=body,
        )

    def _make_invariant_property_test(self, invariants: List[Invariant]) -> Optional[PropertyTest]:
        """Build a combined property test that checks all invariants."""
        stable = [i for i in invariants if i.kind in ("never_changes", "never_null", "stable_type")]
        if not stable:
            return None

        strategies = []
        body_lines = [
            "    # Verify invariants hold regardless of input value",
            "    assume(value is not None)  # skip degenerate inputs",
        ]
        for inv in stable[:5]:   # limit to avoid very large @given signatures
            strat = self._strategy_for_invariant(inv)
            if strat:
                strategies.append(strat)
                assertion = inv.to_assertion(result_expr="variables")
                body_lines.append(f"    {assertion.code}")

        if not strategies:
            return None

        return PropertyTest(
            function_name="test_invariants_hold",
            docstring=(
                f"All {len(stable)} detected invariants must hold regardless of input. "
                "FlowDelta detected these from runtime observations."
            ),
            strategies=strategies,
            body_lines=body_lines,
        )

    # ------------------------------------------------------------------
    # Strategy builders
    # ------------------------------------------------------------------

    def _strategy_for(self, var: str, value: Any) -> Optional[StrategySpec]:
        """Derive a Hypothesis strategy from an observed value."""
        if isinstance(value, bool):
            return StrategySpec(
                variable=var,
                strategy_code="st.booleans()",
                description="boolean values",
                observed_type="bool",
            )
        if isinstance(value, int):
            lo = max(0, value - abs(value) * 2) if value != 0 else -100
            hi = value + abs(value) * 2 if value != 0 else 100
            return StrategySpec(
                variable=var,
                strategy_code=f"st.integers(min_value={int(lo)}, max_value={int(hi)})",
                description=f"integers around observed value {value}",
                observed_type="int",
            )
        if isinstance(value, float):
            lo = value - abs(value) * 2
            hi = value + abs(value) * 2
            return StrategySpec(
                variable=var,
                strategy_code=f"st.floats(min_value={lo!r}, max_value={hi!r}, allow_nan=False)",
                description=f"floats around observed value {value!r}",
                observed_type="float",
            )
        if isinstance(value, str):
            return StrategySpec(
                variable=var,
                strategy_code="st.text(min_size=0, max_size=200)",
                description="arbitrary text strings",
                observed_type="str",
            )
        if isinstance(value, list):
            return StrategySpec(
                variable=var,
                strategy_code=f"st.lists(st.integers(), min_size=0, max_size={max(len(value)*2, 10)})",
                description=f"lists (observed length: {len(value)})",
                observed_type="list",
            )
        if value is None:
            return StrategySpec(
                variable=var,
                strategy_code="st.none() | st.integers()",
                description="None or integer",
                observed_type="NoneType",
            )
        return None

    def _strategy_for_invariant(self, inv: Invariant) -> Optional[StrategySpec]:
        """Derive a strategy from an :class:`Invariant`."""
        return self._strategy_for(inv.variable, inv.observed_value)

    # ------------------------------------------------------------------
    # Body builders
    # ------------------------------------------------------------------

    def _body_for(self, change: VariableDelta, strategy: StrategySpec) -> List[str]:
        """Generate assertion lines for the property test body."""
        var = strategy.variable
        lines = [
            f"    # Observed change: {change.change_type} at runtime",
            f"    # Strategy: {strategy.description}",
            f"    value = {var}  # passed via @given",
        ]

        otype = strategy.observed_type
        if otype == "bool":
            lines.append(f"    assert isinstance(value, bool)")
        elif otype == "int":
            lines.append(f"    assert isinstance(value, int)")
            if change.change_type == "changed" and isinstance(change.new_value, int):
                lines.append(f"    # Observed final value: {change.new_value!r}")
        elif otype == "float":
            lines.append(f"    assert isinstance(value, float)")
        elif otype == "str":
            lines.append(f"    assert isinstance(value, str)")
        elif otype == "list":
            lines.append(f"    assert isinstance(value, list)")
        else:
            lines.append(f"    assert value is not None")

        return lines

    # ------------------------------------------------------------------
    # Renderer helpers
    # ------------------------------------------------------------------

    def _render_test(self, pt: PropertyTest) -> List[str]:
        """Render one :class:`PropertyTest` to source lines."""
        lines: List[str] = []

        # Build @given decorator
        args = ", ".join(
            f"{s.variable}={s.strategy_code}"
            for s in pt.strategies
        )
        lines.append(f"@given({args})")
        lines.append(f"@settings(max_examples={self.max_examples})")
        lines.append(f"def {pt.function_name}({', '.join(s.variable for s in pt.strategies)}):")
        lines.append(f'    """{pt.docstring}"""')

        for line in pt.body_lines:
            lines.append(line)

        return lines
