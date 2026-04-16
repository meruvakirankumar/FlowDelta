"""
Tests for Sprint 3: InvariantDetector, HypothesisTestGenerator, MutationRunner.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.state_tracker.dap_client import StateSnapshot
from src.state_tracker.trace_recorder import FlowTrace
from src.delta_engine.state_diff import StateDiffer, TraceDelta, SnapshotDelta, VariableDelta
from src.test_generator.invariant_detector import InvariantDetector, Invariant
from src.test_generator.hypothesis_gen import HypothesisTestGenerator
from src.test_generator.mutation_runner import MutationRunner, MutationReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_snapshot(locals_: dict, seq: int = 1) -> StateSnapshot:
    return StateSnapshot(
        event="call",
        thread_id=0,
        file="ecommerce.py",
        line=10 + seq,
        function="checkout",
        locals=locals_,
        sequence=seq,
    )


def _make_trace(snapshots_locals: list[dict], flow_id: str = "checkout") -> FlowTrace:
    snaps = [_make_snapshot(loc, i + 1) for i, loc in enumerate(snapshots_locals)]
    return FlowTrace(flow_id=flow_id, run_id="test-run", snapshots=snaps)


def _make_delta(changes: list[tuple]) -> TraceDelta:
    """changes = [(name, change_type, old_val, new_val)]"""
    var_deltas = [
        VariableDelta(
            name=name, change_type=ct,
            old_value=old, new_value=new,
            deep_path=f"root['{name}']",
        )
        for name, ct, old, new in changes
    ]
    sd = SnapshotDelta(
        from_seq=1, to_seq=2,
        from_location="ecommerce.py:10 (checkout)",
        to_location="ecommerce.py:20 (build_cart)",
        changes=var_deltas,
    )
    td = TraceDelta(flow_id="checkout", run_id="test-run")
    td.deltas.append(sd)
    return td


# ===========================================================================
# InvariantDetector
# ===========================================================================

class TestInvariantDetector:

    def test_never_changes_detects_constant_string(self):
        trace = _make_trace([
            {"user_id": "u1", "total": 100},
            {"user_id": "u1", "total": 200},
            {"user_id": "u1", "total": 300},
        ])
        detector = InvariantDetector(min_snapshots=2)
        invs = detector.detect(trace)

        # user_id should have a never_changes invariant (may also have never_null)
        user_id_kinds = {i.kind for i in invs if i.variable == "user_id"}
        assert "never_changes" in user_id_kinds

    def test_changing_variable_not_never_changes(self):
        trace = _make_trace([
            {"total": 100},
            {"total": 200},
            {"total": 300},
        ])
        detector = InvariantDetector(min_snapshots=2)
        invs = detector.detect(trace)
        never_change_vars = {i.variable for i in invs if i.kind == "never_changes"}
        assert "total" not in never_change_vars

    def test_never_null_detected(self):
        trace = _make_trace([
            {"order_id": "ORD-001"},
            {"order_id": "ORD-001"},
        ])
        detector = InvariantDetector(min_snapshots=2)
        invs = detector.detect(trace)
        never_null = {i.variable for i in invs if i.kind == "never_null"}
        # order_id is never null/empty across all snapshots
        assert "order_id" in never_null

    def test_null_variable_not_never_null(self):
        trace = _make_trace([
            {"coupon": None},
            {"coupon": "SAVE10"},
        ])
        detector = InvariantDetector(min_snapshots=2)
        invs = detector.detect(trace)
        never_null = {i.variable for i in invs if i.kind == "never_null"}
        assert "coupon" not in never_null

    def test_monotonic_increase_detected(self):
        trace = _make_trace([
            {"seq": 1},
            {"seq": 2},
            {"seq": 3},
        ])
        detector = InvariantDetector(min_snapshots=2)
        invs = detector.detect(trace)
        kinds = {i.variable: i.kind for i in invs}
        assert "seq" in kinds
        assert kinds["seq"] == "monotonic_increase"

    def test_monotonic_decrease_detected(self):
        trace = _make_trace([
            {"stock": 10},
            {"stock": 7},
            {"stock": 4},
        ])
        detector = InvariantDetector(min_snapshots=2)
        invs = detector.detect(trace)
        kinds = {i.variable: i.kind for i in invs}
        assert kinds.get("stock") == "monotonic_decrease"

    def test_stable_type_detected(self):
        # Non-monotonic floats so monotonic check fails → stable_type fires
        trace = _make_trace([
            {"price": 9.99},
            {"price": 49.99},
            {"price": 19.99},  # drops, so NOT monotonic
        ])
        detector = InvariantDetector(min_snapshots=2)
        invs = detector.detect(trace)
        type_invs = {i.variable: i for i in invs if i.kind == "stable_type"}
        assert "price" in type_invs
        assert type_invs["price"].observed_type == "float"

    def test_to_assertion_never_changes(self):
        inv = Invariant(
            variable="user_id", kind="never_changes",
            observed_value="u1", observed_type="str", snapshot_count=3,
        )
        assertion = inv.to_assertion()
        assert "user_id" in assertion.code
        assert "u1" in assertion.code
        assert assertion.priority == 1

    def test_to_assertion_never_null(self):
        inv = Invariant(
            variable="order_id", kind="never_null",
            observed_value="ORD-1", observed_type="str", snapshot_count=2,
        )
        assertion = inv.to_assertion()
        assert "is not None" in assertion.code

    def test_to_dict(self):
        inv = Invariant(
            variable="x", kind="never_changes",
            observed_value=42, observed_type="int", snapshot_count=5,
        )
        d = inv.to_dict()
        assert d["variable"] == "x"
        assert d["kind"] == "never_changes"
        assert d["observed_value"] == 42
        assert d["snapshot_count"] == 5

    def test_min_snapshots_filter(self):
        trace = _make_trace([{"x": 1}])  # only 1 snapshot
        detector = InvariantDetector(min_snapshots=3)
        invs = detector.detect(trace)
        # x only appears once, should not qualify
        assert not any(i.variable == "x" for i in invs)

    def test_private_vars_skipped(self):
        trace = _make_trace([
            {"_private": "secret", "public": "value"},
            {"_private": "secret", "public": "value"},
        ])
        detector = InvariantDetector(min_snapshots=2)
        invs = detector.detect(trace)
        vars_found = {i.variable for i in invs}
        assert "_private" not in vars_found

    def test_detect_multi_increases_confidence(self):
        trace1 = _make_trace([{"status": "ok"}, {"status": "ok"}])
        trace2 = _make_trace([{"status": "ok"}, {"status": "ok"}])
        detector = InvariantDetector(min_snapshots=2)
        invs = detector.detect_multi([trace1, trace2])
        never_change = {i.variable for i in invs if i.kind == "never_changes"}
        assert "status" in never_change


# ===========================================================================
# HypothesisTestGenerator
# ===========================================================================

class TestHypothesisTestGenerator:

    def test_generate_produces_spec(self):
        td = _make_delta([
            ("total", "changed", 100, 200),
            ("status", "changed", "pending", "confirmed"),
        ])
        gen = HypothesisTestGenerator()
        spec = gen.generate(td)
        assert spec.flow_id == "checkout"
        assert len(spec.tests) > 0

    def test_integer_strategy_generated(self):
        td = _make_delta([("count", "changed", 0, 5)])
        gen = HypothesisTestGenerator()
        spec = gen.generate(td)
        strategies = [s.strategy_code for t in spec.tests for s in t.strategies]
        assert any("st.integers" in s for s in strategies)

    def test_float_strategy_generated(self):
        td = _make_delta([("price", "changed", 9.99, 19.99)])
        gen = HypothesisTestGenerator()
        spec = gen.generate(td)
        strategies = [s.strategy_code for t in spec.tests for s in t.strategies]
        assert any("st.floats" in s for s in strategies)

    def test_string_strategy_generated(self):
        td = _make_delta([("name", "changed", "Alice", "Bob")])
        gen = HypothesisTestGenerator()
        spec = gen.generate(td)
        strategies = [s.strategy_code for t in spec.tests for s in t.strategies]
        assert any("st.text" in s for s in strategies)

    def test_bool_strategy_generated(self):
        td = _make_delta([("active", "changed", False, True)])
        gen = HypothesisTestGenerator()
        spec = gen.generate(td)
        strategies = [s.strategy_code for t in spec.tests for s in t.strategies]
        assert any("st.booleans" in s for s in strategies)

    def test_invariant_property_test_included(self):
        td = _make_delta([("total", "changed", 0, 100)])
        invs = [
            Invariant(variable="user_id", kind="never_changes",
                      observed_value="u1", observed_type="str", snapshot_count=3),
        ]
        gen = HypothesisTestGenerator(include_invariants=True)
        spec = gen.generate(td, invariants=invs)
        names = [t.function_name for t in spec.tests]
        assert "test_invariants_hold" in names

    def test_render_creates_file(self, tmp_path):
        td = _make_delta([("total", "changed", 0, 100)])
        gen = HypothesisTestGenerator(max_examples=50)
        spec = gen.generate(td)
        out = gen.render(spec, output_dir=tmp_path)
        assert out.exists()
        content = out.read_text()
        assert "from hypothesis import given" in content
        assert "@given" in content
        assert "@settings(max_examples=50)" in content

    def test_render_file_has_hypothesis_imports(self, tmp_path):
        td = _make_delta([("x", "changed", 1, 2)])
        gen = HypothesisTestGenerator()
        spec = gen.generate(td)
        out = gen.render(spec, output_dir=tmp_path)
        content = out.read_text()
        assert "import pytest" in content
        assert "from hypothesis import" in content
        assert "from hypothesis import strategies as st" in content


# ===========================================================================
# MutationRunner
# ===========================================================================

class TestMutationRunner:

    def test_report_structure(self):
        report = MutationReport(
            source_file="src.py", test_file="test_src.py",
            total=10, killed=8, survived=2, score=0.8,
        )
        assert report.score == 0.8
        assert report.survived == 2
        d = report.to_dict()
        assert d["score"] == 0.8
        assert "survived_mutants" in d

    def test_summary_contains_key_info(self):
        report = MutationReport(
            source_file="src.py", test_file="test_src.py",
            total=5, killed=4, survived=1, score=0.8,
        )
        s = report.summary()
        assert "Score" in s
        assert "Killed" in s

    def test_suggest_improvements_arithmetic(self):
        from src.test_generator.mutation_runner import MutantResult
        runner = MutationRunner("src.py", "test.py")
        mutant = MutantResult(
            mutant_id="1", status="survived",
            description="Replace + with -",
            file="src.py", line=5,
            original="    total = a + b",
            mutated="    total = a - b",
        )
        sugg = runner._suggest_for_mutant(mutant)
        assert sugg is not None
        assert sugg.line == 5
        assert "assertion" in sugg.suggested_assertion.lower() or "assert" in sugg.suggested_assertion

    def test_suggest_improvements_boolean(self):
        from src.test_generator.mutation_runner import MutantResult
        runner = MutationRunner("src.py", "test.py")
        mutant = MutantResult(
            mutant_id="2", status="survived",
            description="Replace True with False",
            file="src.py", line=10,
            original="    active = True",
            mutated="    active = False",
        )
        sugg = runner._suggest_for_mutant(mutant)
        assert sugg is not None
        assert "bool" in sugg.suggested_assertion.lower() or "True" in sugg.suggested_assertion

    def test_feedback_loop_returns_suggestions_below_threshold(self, tmp_path):
        """When score is below threshold, suggestions are returned."""
        # Create a minimal source and test file that always pass (mutants survive)
        src = tmp_path / "target.py"
        src.write_text("def add(a, b):\n    return a + b\n")
        test = tmp_path / "test_target.py"
        test.write_text(
            "from target import add\n\ndef test_add():\n    assert add(1, 2) is not None\n"
        )
        runner = MutationRunner(
            source_file=src, test_file=test,
            backend="builtin", threshold=0.99,
        )
        report, suggestions = runner.feedback_loop()
        assert isinstance(report, MutationReport)
        assert isinstance(suggestions, list)

    def test_builtin_operators_find_mutations(self, tmp_path):
        src = tmp_path / "calc.py"
        src.write_text("def sub(a, b):\n    return a - b\n")
        test = tmp_path / "test_calc.py"
        test.write_text("def test_placeholder():\n    assert True\n")
        runner = MutationRunner(src, test, backend="builtin")
        report = runner.run()
        # Should find at least one mutation operator match
        assert report.total >= 1
