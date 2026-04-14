"""
Tests for SQLiteQueryAPI.

Uses an in-memory / tmp-dir SQLite database populated via DeltaStore
so no external services are needed.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from src.state_tracker.dap_client import StateSnapshot
from src.state_tracker.trace_recorder import FlowTrace
from src.delta_engine.delta_store import DeltaStore
from src.delta_engine.state_diff import StateDiffer
from src.delta_engine.sqlite_query import SQLiteQueryAPI


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_snap(seq, locals_, fn="fn", line=1):
    return StateSnapshot(
        event="call", thread_id=0, file="app.py",
        line=line, function=fn, locals=locals_, sequence=seq,
    )


def seed_store(store_path: Path, flow_id: str, runs: list, golden_idx: int = 0) -> list:
    """
    Populate a DeltaStore (sqlite) with synthetic runs.

    *runs* is a list of [(locals_before, locals_after), ...] pairs.
    Returns list of run_ids.
    """
    store = DeltaStore(store_path=str(store_path), format="sqlite")
    differ = StateDiffer()
    run_ids = []

    for i, (before, after) in enumerate(runs):
        run_id = str(uuid.uuid4())[:8]
        s1 = make_snap(1, before)
        s2 = make_snap(2, after)
        trace = FlowTrace(flow_id, run_id, [s1, s2])
        store.save_trace(trace, golden=(i == golden_idx))
        td = differ.diff_trace(trace)
        store.save_delta(td, run_id)
        run_ids.append(run_id)

    return run_ids


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def populated_store(tmp_path):
    """Create a SQLite store with two flows and several runs."""
    flow_a_runs = [
        ({"status": "pending", "total": 0},   {"status": "confirmed", "total": 99}),
        ({"status": "pending", "total": 0},   {"status": "confirmed", "total": 110}),
        ({"status": "pending", "total": 0},   {"status": "confirmed", "total": 99}),
    ]
    flow_b_runs = [
        ({"verified": False}, {"verified": True}),
        ({"verified": False}, {"verified": True, "token": "ABC"}),
    ]
    ids_a = seed_store(tmp_path, "checkout", flow_a_runs, golden_idx=0)
    ids_b = seed_store(tmp_path, "registration", flow_b_runs, golden_idx=0)
    return tmp_path, ids_a, ids_b


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestSQLiteQueryAPIInit:
    def test_raises_if_db_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="No FlowDelta SQLite"):
            SQLiteQueryAPI(store_path=str(tmp_path / "nonexistent"))

    def test_opens_successfully(self, populated_store):
        store_path, _, _ = populated_store
        api = SQLiteQueryAPI(store_path=str(store_path))
        api.close()


# ---------------------------------------------------------------------------
# flows_summary()
# ---------------------------------------------------------------------------

class TestFlowsSummary:
    def test_returns_both_flows(self, populated_store):
        store_path, _, _ = populated_store
        with SQLiteQueryAPI(store_path=str(store_path)) as api:
            summary = api.flows_summary()
        flow_ids = {r["flow_id"] for r in summary}
        assert "checkout" in flow_ids
        assert "registration" in flow_ids

    def test_run_counts_correct(self, populated_store):
        store_path, ids_a, _ = populated_store
        with SQLiteQueryAPI(store_path=str(store_path)) as api:
            summary = api.flows_summary()
        checkout = next(r for r in summary if r["flow_id"] == "checkout")
        assert checkout["total_runs"] == 3
        assert checkout["golden_runs"] == 1

    def test_total_changes_positive(self, populated_store):
        store_path, _, _ = populated_store
        with SQLiteQueryAPI(store_path=str(store_path)) as api:
            summary = api.flows_summary()
        for row in summary:
            assert row["total_changes"] >= 0


# ---------------------------------------------------------------------------
# run_history()
# ---------------------------------------------------------------------------

class TestRunHistory:
    def test_returns_runs_for_flow(self, populated_store):
        store_path, ids_a, _ = populated_store
        with SQLiteQueryAPI(store_path=str(store_path)) as api:
            history = api.run_history("checkout")
        assert len(history) == 3

    def test_golden_only_filter(self, populated_store):
        store_path, _, _ = populated_store
        with SQLiteQueryAPI(store_path=str(store_path)) as api:
            history = api.run_history("checkout", golden_only=True)
        assert all(r["golden"] is True for r in history)
        assert len(history) == 1

    def test_limit_respected(self, populated_store):
        store_path, _, _ = populated_store
        with SQLiteQueryAPI(store_path=str(store_path)) as api:
            history = api.run_history("checkout", limit=2)
        assert len(history) <= 2

    def test_snapshot_count_present(self, populated_store):
        store_path, _, _ = populated_store
        with SQLiteQueryAPI(store_path=str(store_path)) as api:
            history = api.run_history("checkout")
        for r in history:
            assert "snapshot_count" in r
            assert r["snapshot_count"] >= 0


# ---------------------------------------------------------------------------
# hot_variables()
# ---------------------------------------------------------------------------

class TestHotVariables:
    def test_returns_most_changed_first(self, populated_store):
        store_path, _, _ = populated_store
        with SQLiteQueryAPI(store_path=str(store_path)) as api:
            hot = api.hot_variables("checkout")
        assert len(hot) > 0
        assert hot[0]["change_count"] >= hot[-1]["change_count"]

    def test_includes_status_and_total(self, populated_store):
        store_path, _, _ = populated_store
        with SQLiteQueryAPI(store_path=str(store_path)) as api:
            hot = api.hot_variables("checkout")
        var_names = {r["variable"] for r in hot}
        assert "status" in var_names or "total" in var_names

    def test_limit_respected(self, populated_store):
        store_path, _, _ = populated_store
        with SQLiteQueryAPI(store_path=str(store_path)) as api:
            hot = api.hot_variables("checkout", limit=1)
        assert len(hot) == 1


# ---------------------------------------------------------------------------
# regression_trend()
# ---------------------------------------------------------------------------

class TestRegressionTrend:
    def test_returns_one_entry_per_run(self, populated_store):
        store_path, ids_a, _ = populated_store
        with SQLiteQueryAPI(store_path=str(store_path)) as api:
            trend = api.regression_trend("checkout")
        assert len(trend) == len(ids_a)

    def test_each_entry_has_change_count(self, populated_store):
        store_path, _, _ = populated_store
        with SQLiteQueryAPI(store_path=str(store_path)) as api:
            trend = api.regression_trend("checkout")
        for entry in trend:
            assert "change_count" in entry
            assert entry["change_count"] >= 0


# ---------------------------------------------------------------------------
# search_changes()
# ---------------------------------------------------------------------------

class TestSearchChanges:
    def test_finds_status_changes(self, populated_store):
        store_path, _, _ = populated_store
        with SQLiteQueryAPI(store_path=str(store_path)) as api:
            results = api.search_changes("status", flow_id="checkout")
        assert len(results) > 0
        assert all(r["flow_id"] == "checkout" for r in results)

    def test_change_type_filter(self, populated_store):
        store_path, _, _ = populated_store
        with SQLiteQueryAPI(store_path=str(store_path)) as api:
            results = api.search_changes("status", flow_id="checkout", change_type="changed")
        assert all(r["change_type"] == "changed" for r in results)

    def test_unknown_variable_returns_empty(self, populated_store):
        store_path, _, _ = populated_store
        with SQLiteQueryAPI(store_path=str(store_path)) as api:
            results = api.search_changes("nonexistent_variable_xyz")
        assert results == []


# ---------------------------------------------------------------------------
# compare_runs()
# ---------------------------------------------------------------------------

class TestCompareRuns:
    def test_in_both_non_empty_for_identical_runs(self, tmp_path):
        """Two runs with the same change should share that path."""
        same_runs = [
            ({"x": 1}, {"x": 2}),
            ({"x": 1}, {"x": 2}),
        ]
        ids = seed_store(tmp_path, "test-flow", same_runs)
        with SQLiteQueryAPI(store_path=str(tmp_path)) as api:
            result = api.compare_runs(ids[0], ids[1])
        assert len(result["in_both"]) > 0

    def test_only_in_a_detects_unique_change(self, tmp_path):
        """Run A changes 'total', run B changes 'status'."""
        run_a = [({"total": 0}, {"total": 99})]
        run_b = [({"status": "pending"}, {"status": "confirmed"})]
        ids_a = seed_store(tmp_path, "flow-a", run_a)
        # Need a second store for flow-b to avoid key collision; reuse same DB
        store = DeltaStore(store_path=str(tmp_path), format="sqlite")
        differ = StateDiffer()
        rid_b = str(uuid.uuid4())[:8]
        s1 = make_snap(1, {"status": "pending"})
        s2 = make_snap(2, {"status": "confirmed"})
        trace_b = FlowTrace("flow-b", rid_b, [s1, s2])
        store.save_trace(trace_b)
        store.save_delta(differ.diff_trace(trace_b), rid_b)

        with SQLiteQueryAPI(store_path=str(tmp_path)) as api:
            result = api.compare_runs(ids_a[0], rid_b)
        assert len(result["only_in_a"]) > 0 or len(result["only_in_b"]) > 0


# ---------------------------------------------------------------------------
# delete_run()
# ---------------------------------------------------------------------------

class TestDeleteRun:
    def test_deletes_trace_and_delta(self, populated_store):
        store_path, ids_a, _ = populated_store
        target = ids_a[0]
        with SQLiteQueryAPI(store_path=str(store_path)) as api:
            before = api.run_history("checkout")
            deleted = api.delete_run(target)
            after = api.run_history("checkout")
        assert deleted >= 1
        assert len(after) == len(before) - 1

    def test_delete_nonexistent_returns_zero(self, populated_store):
        store_path, _, _ = populated_store
        with SQLiteQueryAPI(store_path=str(store_path)) as api:
            deleted = api.delete_run("no-such-id")
        assert deleted == 0


# ---------------------------------------------------------------------------
# vacuum()
# ---------------------------------------------------------------------------

class TestVacuum:
    def test_vacuum_runs_without_error(self, populated_store):
        store_path, _, _ = populated_store
        with SQLiteQueryAPI(store_path=str(store_path)) as api:
            api.vacuum()   # should not raise
