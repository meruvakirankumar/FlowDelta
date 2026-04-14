"""
Tests for the delta engine – verifiable without external services.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from src.state_tracker.dap_client import StateSnapshot
from src.state_tracker.trace_recorder import FlowTrace
from src.delta_engine import StateDiffer, TraceDelta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_snapshot(seq: int, locals_: dict, fn: str = "my_func", line: int = 1) -> StateSnapshot:
    return StateSnapshot(
        event="call",
        thread_id=0,
        file="app.py",
        line=line,
        function=fn,
        locals=locals_,
        sequence=seq,
    )


# ---------------------------------------------------------------------------
# StateDiffer tests
# ---------------------------------------------------------------------------

class TestStateDiffer:
    def test_no_change(self):
        s1 = make_snapshot(1, {"x": 1})
        s2 = make_snapshot(2, {"x": 1})
        trace = FlowTrace("flow", "run1", [s1, s2])
        td = StateDiffer().diff_trace(trace)
        assert td.total_changes == 0

    def test_value_changed(self):
        s1 = make_snapshot(1, {"total": 0})
        s2 = make_snapshot(2, {"total": 99})
        trace = FlowTrace("flow", "run1", [s1, s2])
        td = StateDiffer().diff_trace(trace)
        assert td.total_changes == 1
        change = td.deltas[0].changes[0]
        assert change.name == "total"
        assert change.change_type == "changed"
        assert change.old_value == 0
        assert change.new_value == 99

    def test_variable_added(self):
        s1 = make_snapshot(1, {})
        s2 = make_snapshot(2, {"order_id": "ORD-001"})
        trace = FlowTrace("flow", "run1", [s1, s2])
        td = StateDiffer().diff_trace(trace)
        names = [c.name for c in td.deltas[0].changes]
        assert "order_id" in names

    def test_variable_removed(self):
        s1 = make_snapshot(1, {"temp_token": "ABC123"})
        s2 = make_snapshot(2, {})
        trace = FlowTrace("flow", "run1", [s1, s2])
        td = StateDiffer().diff_trace(trace)
        names = [c.name for c in td.deltas[0].changes]
        assert "temp_token" in names

    def test_nested_change(self):
        s1 = make_snapshot(1, {"cart": {"total": 50.0, "items": []}})
        s2 = make_snapshot(2, {"cart": {"total": 40.0, "items": [{"name": "Laptop"}]}})
        trace = FlowTrace("flow", "run1", [s1, s2])
        td = StateDiffer().diff_trace(trace)
        assert td.total_changes > 0


# ---------------------------------------------------------------------------
# TraceDelta summary tests
# ---------------------------------------------------------------------------

class TestTraceDelta:
    def test_total_changes_sum(self):
        s1 = make_snapshot(1, {"a": 1, "b": 2})
        s2 = make_snapshot(2, {"a": 9, "b": 2})
        s3 = make_snapshot(3, {"a": 9, "b": 99})
        trace = FlowTrace("flow", "run1", [s1, s2, s3])
        td = StateDiffer().diff_trace(trace)
        assert td.total_changes == 2

    def test_snapshot_delta_has_change_detected(self):
        s1 = make_snapshot(1, {"status": "pending"})
        s2 = make_snapshot(2, {"status": "confirmed"})
        trace = FlowTrace("flow", "run1", [s1, s2])
        td = StateDiffer().diff_trace(trace)
        assert td.deltas[0].has_changes

    def test_empty_trace_produces_no_deltas(self):
        trace = FlowTrace("flow", "run1", [])
        td = StateDiffer().diff_trace(trace)
        assert td.deltas == []
