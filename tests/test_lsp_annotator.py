"""
Tests for LSPAnnotator.

Mocks the LSPClient so no real language server is required.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from src.state_tracker.dap_client import StateSnapshot, StackFrame
from src.state_tracker.lsp_annotator import LSPAnnotator
from src.state_tracker.trace_recorder import FlowTrace


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_snapshot(seq, locals_, file="app.py", line=5, fn="my_fn"):
    return StateSnapshot(
        event="call", thread_id=0, file=file,
        line=line, function=fn, locals=locals_, sequence=seq,
    )


def make_mock_lsp(hover_result: str = "int"):
    lsp = AsyncMock()
    lsp.open_document = AsyncMock()
    lsp.type_at = AsyncMock(return_value=hover_result)
    lsp.hover = AsyncMock(return_value=f"count: {hover_result}")
    return lsp


# ---------------------------------------------------------------------------
# _find_column
# ---------------------------------------------------------------------------

class TestFindColumn:
    def _annotator(self):
        return LSPAnnotator(make_mock_lsp())

    def test_finds_variable_on_same_line(self):
        ann = self._annotator()
        lines = ["", "", "", "", "    count = 0"]   # line 5 (1-based)
        col = ann._find_column(lines, snap_line=5, var_name="count")
        assert col is not None
        assert col >= 1

    def test_finds_variable_in_window(self):
        ann = self._annotator()
        lines = ["", "    total = compute()", "", "", ""]
        # snap_line=5, variable on line 2
        col = ann._find_column(lines, snap_line=5, var_name="total")
        assert col is not None

    def test_returns_none_when_not_found(self):
        ann = self._annotator()
        lines = ["x = 1", "y = 2", "z = 3"]
        col = ann._find_column(lines, snap_line=3, var_name="missing_var")
        assert col is None

    def test_does_not_match_partial_name(self):
        """'my_count' should not match when looking for 'count'."""
        ann = self._annotator()
        lines = ["    my_count = 0"]
        col = ann._find_column(lines, snap_line=1, var_name="count")
        # Should be None because 'count' is part of 'my_count' – word boundary
        assert col is None


# ---------------------------------------------------------------------------
# _runtime_type
# ---------------------------------------------------------------------------

class TestRuntimeType:
    def test_int(self):
        assert LSPAnnotator._runtime_type(42) == "int"

    def test_float(self):
        assert LSPAnnotator._runtime_type(3.14) == "float"

    def test_str(self):
        assert LSPAnnotator._runtime_type("hello") == "str"

    def test_list(self):
        assert LSPAnnotator._runtime_type([]) == "list"

    def test_none(self):
        assert LSPAnnotator._runtime_type(None) == "None"

    def test_bool(self):
        assert LSPAnnotator._runtime_type(True) == "bool"

    def test_serialized_object(self):
        obj = {"__type__": "Cart", "items": []}
        assert LSPAnnotator._runtime_type(obj) == "Cart"


# ---------------------------------------------------------------------------
# annotate() – with mocked LSP
# ---------------------------------------------------------------------------

class TestAnnotate:
    @pytest.mark.asyncio
    async def test_annotate_returns_type_for_variable(self, tmp_path):
        source = tmp_path / "app.py"
        source.write_text("def fn():\n    count = 0\n    return count\n")

        lsp = make_mock_lsp(hover_result="int")
        ann = LSPAnnotator(lsp)

        snap = make_snapshot(1, {"count": 0}, file=str(source), line=2)
        result = await ann.annotate(snap)

        assert "count" in result
        assert result["count"] == "int"

    @pytest.mark.asyncio
    async def test_annotate_skips_builtins(self, tmp_path):
        source = tmp_path / "app.py"
        source.write_text("def fn():\n    x = True\n")

        lsp = make_mock_lsp()
        ann = LSPAnnotator(lsp)
        snap = make_snapshot(1, {"True": True, "x": 1}, file=str(source), line=2)
        result = await ann.annotate(snap)

        assert "True" not in result   # skipped as builtin
        assert "x" in result

    @pytest.mark.asyncio
    async def test_annotate_falls_back_to_runtime_type_on_lsp_error(self, tmp_path):
        source = tmp_path / "app.py"
        source.write_text("def fn():\n    items = []\n")

        lsp = AsyncMock()
        lsp.open_document = AsyncMock()
        lsp.type_at = AsyncMock(side_effect=Exception("LSP timeout"))
        ann = LSPAnnotator(lsp)

        snap = make_snapshot(1, {"items": []}, file=str(source), line=2)
        result = await ann.annotate(snap)

        # Should fall back to runtime type
        assert result.get("items") == "list"

    @pytest.mark.asyncio
    async def test_annotate_returns_empty_for_missing_file(self):
        lsp = make_mock_lsp()
        ann = LSPAnnotator(lsp)
        snap = make_snapshot(1, {"x": 1}, file="/nonexistent/path.py", line=1)
        result = await ann.annotate(snap)
        assert result == {}


# ---------------------------------------------------------------------------
# annotate_trace()
# ---------------------------------------------------------------------------

class TestAnnotateTrace:
    @pytest.mark.asyncio
    async def test_annotate_trace_attaches_types_to_snapshots(self, tmp_path):
        source = tmp_path / "app.py"
        source.write_text("def fn(x):\n    y = x + 1\n    return y\n")

        lsp = make_mock_lsp(hover_result="int")
        ann = LSPAnnotator(lsp)

        s1 = make_snapshot(1, {"x": 1}, file=str(source), line=2)
        s2 = make_snapshot(2, {"y": 2}, file=str(source), line=3)
        trace = FlowTrace("test-flow", "r1", [s1, s2])

        results = await ann.annotate_trace(trace)

        assert len(results) == 2
        # Types should be attached to snapshots in-place
        assert hasattr(s1, "types")
        assert hasattr(s2, "types")
        lsp.open_document.assert_called_once()   # same file, opened once

    @pytest.mark.asyncio
    async def test_annotate_trace_handles_multiple_files(self, tmp_path):
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("x = 1\n")
        f2.write_text("y = 2\n")

        lsp = make_mock_lsp()
        ann = LSPAnnotator(lsp)

        s1 = make_snapshot(1, {"x": 1}, file=str(f1), line=1)
        s2 = make_snapshot(2, {"y": 2}, file=str(f2), line=1)
        trace = FlowTrace("multi", "r1", [s1, s2])

        await ann.annotate_trace(trace)

        assert lsp.open_document.call_count == 2   # two distinct files
