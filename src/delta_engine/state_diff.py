"""
State Diff Engine – Phase 3 of FlowDelta.

Computes structured deltas between consecutive :class:`StateSnapshot` objects
in a :class:`FlowTrace`.  Each delta records exactly what changed, was added,
or was removed in the local variable scope as execution advanced from one
captured point to the next.

The output is a :class:`TraceDelta` — a timeline of :class:`SnapshotDelta`
objects that can be:
  - Persisted to the delta store
  - Fed into the test generator to produce assertions
  - Visualized as a line-by-line state change report

Uses ``deepdiff`` for deep structural comparison with configurable options.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from deepdiff import DeepDiff

from ..state_tracker.dap_client import StateSnapshot
from ..state_tracker.trace_recorder import FlowTrace


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class VariableDelta:
    """Change record for a single variable between two snapshots."""
    name: str
    change_type: str          # "added" | "removed" | "changed" | "type_changed"
    old_value: Any = None
    new_value: Any = None
    old_type: Optional[str] = None
    new_type: Optional[str] = None
    deep_path: str = ""       # e.g. "root['cart']['items'][2]"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "change_type": self.change_type,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "old_type": self.old_type,
            "new_type": self.new_type,
            "deep_path": self.deep_path,
        }


@dataclass
class SnapshotDelta:
    """
    Delta between snapshot[i-1] and snapshot[i] in a trace.

    Attributes
    ----------
    from_seq : int
        Sequence number of the *before* snapshot.
    to_seq : int
        Sequence number of the *after* snapshot.
    from_location : str
        ``file:line (function)`` of the *before* snapshot.
    to_location : str
        ``file:line (function)`` of the *after* snapshot.
    changes : list[VariableDelta]
        All variable-level changes detected.
    raw_diff : dict
        Raw DeepDiff output for advanced consumers.
    """
    from_seq: int
    to_seq: int
    from_location: str
    to_location: str
    changes: List[VariableDelta] = field(default_factory=list)
    raw_diff: dict = field(default_factory=dict)

    @property
    def has_changes(self) -> bool:
        return len(self.changes) > 0

    def summary(self) -> str:
        if not self.has_changes:
            return f"  {self.from_location} → {self.to_location}: (no change)"
        lines = [f"  {self.from_location} → {self.to_location}:"]
        for c in self.changes:
            if c.change_type == "changed":
                lines.append(f"    ~ {c.name}: {c.old_value!r} → {c.new_value!r}")
            elif c.change_type == "added":
                lines.append(f"    + {c.name}: {c.new_value!r}")
            elif c.change_type == "removed":
                lines.append(f"    - {c.name}: {c.old_value!r}")
            elif c.change_type == "type_changed":
                lines.append(f"    T {c.name}: {c.old_type} → {c.new_type}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "from_seq": self.from_seq,
            "to_seq": self.to_seq,
            "from_location": self.from_location,
            "to_location": self.to_location,
            "changes": [c.to_dict() for c in self.changes],
        }


@dataclass
class TraceDelta:
    """
    Complete delta timeline for one :class:`FlowTrace`.

    One :class:`SnapshotDelta` per consecutive snapshot pair.
    """
    flow_id: str
    run_id: str
    deltas: List[SnapshotDelta] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return sum(len(d.changes) for d in self.deltas)

    def print_report(self) -> None:
        print(f"\n=== Delta Report: {self.flow_id} / run={self.run_id} ===")
        print(f"  {len(self.deltas)} transitions, {self.total_changes} variable changes\n")
        for d in self.deltas:
            print(d.summary())

    def to_dict(self) -> dict:
        return {
            "flow_id": self.flow_id,
            "run_id": self.run_id,
            "deltas": [d.to_dict() for d in self.deltas],
        }


# ---------------------------------------------------------------------------
# Differ
# ---------------------------------------------------------------------------

class StateDiffer:
    """
    Converts a :class:`FlowTrace` into a :class:`TraceDelta`.

    Parameters
    ----------
    ignore_order : bool
        If ``True``, list ordering differences are ignored (useful for sets).
    significant_digits : int
        Number of significant digits for float comparison.
    ignore_keys : list[str]
        Variable names to exclude from diffing (e.g. loop counters).
    """

    def __init__(
        self,
        ignore_order: bool = False,
        significant_digits: int = 5,
        ignore_keys: Optional[List[str]] = None,
    ) -> None:
        self.ignore_order = ignore_order
        self.significant_digits = significant_digits
        self.ignore_keys = set(ignore_keys or [])

    def diff_trace(self, trace: FlowTrace) -> TraceDelta:
        """Compute :class:`TraceDelta` for an entire :class:`FlowTrace`."""
        td = TraceDelta(flow_id=trace.flow_id, run_id=trace.run_id)
        snapshots = trace.snapshots
        for i in range(1, len(snapshots)):
            prev = snapshots[i - 1]
            curr = snapshots[i]
            delta = self.diff_snapshots(prev, curr)
            td.deltas.append(delta)
        return td

    def diff_snapshots(
        self,
        before: StateSnapshot,
        after: StateSnapshot,
    ) -> SnapshotDelta:
        """Compute :class:`SnapshotDelta` between two :class:`StateSnapshot` objects."""
        before_locals = self._filter(before.locals)
        after_locals = self._filter(after.locals)

        raw = DeepDiff(
            before_locals,
            after_locals,
            ignore_order=self.ignore_order,
            significant_digits=self.significant_digits,
            verbose_level=2,
        ).to_dict()

        changes = self._parse_deepdiff(raw, before_locals, after_locals)

        return SnapshotDelta(
            from_seq=before.sequence,
            to_seq=after.sequence,
            from_location=self._loc(before),
            to_location=self._loc(after),
            changes=changes,
            raw_diff=raw,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _filter(self, locals_: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in locals_.items() if k not in self.ignore_keys}

    @staticmethod
    def _loc(snap: StateSnapshot) -> str:
        fname = snap.file.split("/")[-1].split("\\")[-1]
        return f"{fname}:{snap.line} ({snap.function})"

    def _parse_deepdiff(
        self,
        raw: dict,
        before: Dict[str, Any],
        after: Dict[str, Any],
    ) -> List[VariableDelta]:
        changes: List[VariableDelta] = []

        # Values changed
        for path, change in raw.get("values_changed", {}).items():
            name = self._extract_var_name(path)
            changes.append(VariableDelta(
                name=name,
                change_type="changed",
                old_value=change.get("old_value"),
                new_value=change.get("new_value"),
                deep_path=path,
            ))

        # Type changed
        for path, change in raw.get("type_changes", {}).items():
            name = self._extract_var_name(path)
            changes.append(VariableDelta(
                name=name,
                change_type="type_changed",
                old_value=change.get("old_value"),
                new_value=change.get("new_value"),
                old_type=change.get("old_type", type(change.get("old_value")).__name__),
                new_type=change.get("new_type", type(change.get("new_value")).__name__),
                deep_path=path,
            ))

        # Dictionary items added
        for path, value in raw.get("dictionary_item_added", {}).items():
            name = self._extract_var_name(path)
            changes.append(VariableDelta(
                name=name,
                change_type="added",
                new_value=value,
                deep_path=path,
            ))

        # Dictionary items removed
        for path, value in raw.get("dictionary_item_removed", {}).items():
            name = self._extract_var_name(path)
            changes.append(VariableDelta(
                name=name,
                change_type="removed",
                old_value=value,
                deep_path=path,
            ))

        # Iterable items added / removed
        for path in raw.get("iterable_item_added", {}):
            name = self._extract_var_name(path)
            changes.append(VariableDelta(
                name=name,
                change_type="added",
                new_value=raw["iterable_item_added"][path],
                deep_path=path,
            ))
        for path in raw.get("iterable_item_removed", {}):
            name = self._extract_var_name(path)
            changes.append(VariableDelta(
                name=name,
                change_type="removed",
                old_value=raw["iterable_item_removed"][path],
                deep_path=path,
            ))

        return changes

    @staticmethod
    def _extract_var_name(deepdiff_path: str) -> str:
        """Extract the top-level variable name from a DeepDiff path string."""
        # Path looks like: "root['cart']" or "root['cart']['items'][0]"
        import re
        m = re.match(r"root\['?([^'\]]+)'?\]", deepdiff_path)
        return m.group(1) if m else deepdiff_path
