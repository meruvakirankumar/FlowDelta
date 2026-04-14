"""
Tests for the Assertion Generator.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from src.state_tracker.dap_client import StateSnapshot
from src.state_tracker.trace_recorder import FlowTrace
from src.delta_engine import StateDiffer
from src.test_generator import AssertionGenerator


def make_snap(seq, locals_):
    return StateSnapshot(event="call", thread_id=0, file="app.py",
                         line=seq, function="fn", locals=locals_, sequence=seq)


class TestAssertionGenerator:
    def test_generates_equality_assertion(self):
        s1 = make_snap(1, {"status": "pending"})
        s2 = make_snap(2, {"status": "confirmed"})
        trace = FlowTrace("checkout", "r1", [s1, s2])
        td = StateDiffer().diff_trace(trace)
        spec = AssertionGenerator().generate(td)
        codes = [a.code for a in spec.all_assertions]
        assert any("confirmed" in c for c in codes)

    def test_generates_int_assertion(self):
        s1 = make_snap(1, {"count": 0})
        s2 = make_snap(2, {"count": 5})
        trace = FlowTrace("add-items", "r1", [s1, s2])
        td = StateDiffer().diff_trace(trace)
        spec = AssertionGenerator().generate(td)
        codes = [a.code for a in spec.all_assertions]
        assert any("5" in c for c in codes)

    def test_no_assertions_when_no_changes(self):
        s1 = make_snap(1, {"x": 42})
        s2 = make_snap(2, {"x": 42})
        trace = FlowTrace("stable", "r1", [s1, s2])
        td = StateDiffer().diff_trace(trace)
        spec = AssertionGenerator().generate(td)
        assert spec.all_assertions == []

    def test_none_assertion(self):
        s1 = make_snap(1, {"err": "oops"})
        s2 = make_snap(2, {"err": None})
        trace = FlowTrace("clear-err", "r1", [s1, s2])
        td = StateDiffer().diff_trace(trace)
        spec = AssertionGenerator().generate(td)
        codes = [a.code for a in spec.all_assertions]
        assert any("None" in c for c in codes)
