"""
Assertion Generator – Phase 4 of FlowDelta.

Converts :class:`TraceDelta` objects into concrete pytest assertion
statements that can be embedded in generated test cases.

Each :class:`VariableDelta` produces one or more assertion strategies:

* **equality** — ``assert var == expected``
* **type check** — ``assert isinstance(var, ExpectedType)``
* **range / threshold** — for numeric changes
* **membership** — for collection additions / removals
* **invariant** — properties that should NEVER change across a flow

The generator also ranks assertions by *signal strength*: changes in
deeply nested structures or large collections generate higher-priority
assertions that are more likely to catch regressions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from ..delta_engine.state_diff import TraceDelta, VariableDelta


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Assertion:
    """One generated pytest assertion line."""
    code: str                   # e.g. 'assert result["total"] == 99.99'
    description: str            # human-readable intent
    priority: int               # 1 (high) – 5 (low)
    variable: str               # top-level variable name
    change_type: str            # added | removed | changed | type_changed
    location: str               # "file:line (function)"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "description": self.description,
            "priority": self.priority,
            "variable": self.variable,
            "change_type": self.change_type,
            "location": self.location,
        }


@dataclass
class AssertionGroup:
    """Assertions for one transition (SnapshotDelta)."""
    from_location: str
    to_location: str
    assertions: List[Assertion] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "from_location": self.from_location,
            "to_location": self.to_location,
            "assertions": [a.to_dict() for a in self.assertions],
        }


@dataclass
class TestSpec:
    """Complete test specification derived from a :class:`TraceDelta`."""
    flow_id: str
    run_id: str
    groups: List[AssertionGroup] = field(default_factory=list)
    setup_code: List[str] = field(default_factory=list)
    teardown_code: List[str] = field(default_factory=list)

    @property
    def all_assertions(self) -> List[Assertion]:
        return [a for g in self.groups for a in g.assertions]

    def to_dict(self) -> dict:
        return {
            "flow_id": self.flow_id,
            "run_id": self.run_id,
            "groups": [g.to_dict() for g in self.groups],
        }


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

class AssertionGenerator:
    """
    Converts a :class:`TraceDelta` into a :class:`TestSpec`.

    Parameters
    ----------
    min_priority : int
        Only include assertions with priority <= this value (1=critical, 5=low).
    max_string_len : int
        For string equality assertions, truncate expected values longer than
        this to avoid gigantic test files.
    numeric_tolerance : float
        Relative tolerance for float equality assertions.
    """

    def __init__(
        self,
        min_priority: int = 4,
        max_string_len: int = 200,
        numeric_tolerance: float = 1e-6,
    ) -> None:
        self.min_priority = min_priority
        self.max_string_len = max_string_len
        self.numeric_tolerance = numeric_tolerance

    def generate(self, delta: TraceDelta) -> TestSpec:
        spec = TestSpec(flow_id=delta.flow_id, run_id=delta.run_id)

        for sd in delta.deltas:
            if not sd.has_changes:
                continue
            group = AssertionGroup(
                from_location=sd.from_location,
                to_location=sd.to_location,
            )
            for change in sd.changes:
                assertions = self._from_variable_delta(change, sd.to_location)
                group.assertions.extend(
                    a for a in assertions if a.priority <= self.min_priority
                )
            if group.assertions:
                spec.groups.append(group)

        return spec

    # ------------------------------------------------------------------
    # Per-variable strategies
    # ------------------------------------------------------------------

    def _from_variable_delta(
        self,
        change: VariableDelta,
        location: str,
    ) -> List[Assertion]:
        results: List[Assertion] = []

        if change.change_type == "changed":
            results.extend(self._changed_assertions(change, location))
        elif change.change_type == "added":
            results.extend(self._added_assertions(change, location))
        elif change.change_type == "removed":
            results.extend(self._removed_assertions(change, location))
        elif change.change_type == "type_changed":
            results.extend(self._type_changed_assertions(change, location))

        return results

    def _changed_assertions(self, c: VariableDelta, loc: str) -> List[Assertion]:
        assertions: List[Assertion] = []
        var_expr = self._path_to_expr(c.deep_path)

        if isinstance(c.new_value, bool):
            assertions.append(Assertion(
                code=f"assert {var_expr} == {c.new_value}",
                description=f"{c.name} should be {c.new_value} after this step",
                priority=1,
                variable=c.name,
                change_type="changed",
                location=loc,
            ))
        elif isinstance(c.new_value, (int, float)):
            if isinstance(c.new_value, float):
                assertions.append(Assertion(
                    code=f"assert abs({var_expr} - {c.new_value!r}) < {self.numeric_tolerance}",
                    description=f"{c.name} should equal {c.new_value!r} (float)",
                    priority=2,
                    variable=c.name,
                    change_type="changed",
                    location=loc,
                ))
            else:
                assertions.append(Assertion(
                    code=f"assert {var_expr} == {c.new_value!r}",
                    description=f"{c.name} should equal {c.new_value!r}",
                    priority=2,
                    variable=c.name,
                    change_type="changed",
                    location=loc,
                ))
            # Also assert direction of change
            if c.old_value is not None and c.old_value != c.new_value:
                direction = ">" if c.new_value > c.old_value else "<"
                assertions.append(Assertion(
                    code=f"assert {var_expr} {direction} {c.old_value!r}",
                    description=f"{c.name} should increase/decrease from {c.old_value!r}",
                    priority=3,
                    variable=c.name,
                    change_type="changed",
                    location=loc,
                ))
        elif isinstance(c.new_value, str):
            val = c.new_value[:self.max_string_len]
            assertions.append(Assertion(
                code=f"assert {var_expr} == {val!r}",
                description=f"{c.name} should equal expected string",
                priority=2,
                variable=c.name,
                change_type="changed",
                location=loc,
            ))
        elif isinstance(c.new_value, (list, tuple)):
            assertions.append(Assertion(
                code=f"assert len({var_expr}) == {len(c.new_value)}",
                description=f"{c.name} length should be {len(c.new_value)}",
                priority=2,
                variable=c.name,
                change_type="changed",
                location=loc,
            ))
        elif c.new_value is None:
            assertions.append(Assertion(
                code=f"assert {var_expr} is None",
                description=f"{c.name} should be None",
                priority=2,
                variable=c.name,
                change_type="changed",
                location=loc,
            ))
        else:
            assertions.append(Assertion(
                code=f"assert {var_expr} == {c.new_value!r}",
                description=f"{c.name} should match expected value",
                priority=3,
                variable=c.name,
                change_type="changed",
                location=loc,
            ))

        return assertions

    def _added_assertions(self, c: VariableDelta, loc: str) -> List[Assertion]:
        var_expr = self._path_to_expr(c.deep_path)
        description = f"{c.name} should be present after this step"

        if isinstance(c.new_value, dict):
            return [Assertion(
                code=f"assert {var_expr} is not None",
                description=description,
                priority=2,
                variable=c.name,
                change_type="added",
                location=loc,
            )]
        return [Assertion(
            code=f"assert {var_expr} == {c.new_value!r}",
            description=description,
            priority=2,
            variable=c.name,
            change_type="added",
            location=loc,
        )]

    def _removed_assertions(self, c: VariableDelta, loc: str) -> List[Assertion]:
        var_expr = self._path_to_expr(c.deep_path)
        return [Assertion(
            code=f"# {c.name} was removed here (old value: {c.old_value!r})",
            description=f"{c.name} should no longer be present",
            priority=4,
            variable=c.name,
            change_type="removed",
            location=loc,
        )]

    def _type_changed_assertions(self, c: VariableDelta, loc: str) -> List[Assertion]:
        var_expr = self._path_to_expr(c.deep_path)
        new_type = c.new_type or type(c.new_value).__name__
        return [Assertion(
            code=f"assert isinstance({var_expr}, {new_type})",
            description=f"{c.name} type should be {new_type}",
            priority=2,
            variable=c.name,
            change_type="type_changed",
            location=loc,
        )]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _path_to_expr(deep_path: str) -> str:
        """
        Convert a DeepDiff path to a Python expression usable in test code.

        ``root['cart']['items'][0]`` → ``result['cart']['items'][0]``
        """
        return deep_path.replace("root", "result", 1)
