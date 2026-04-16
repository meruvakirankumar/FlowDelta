"""
Sprint 4 tests — OpenTelemetry export, trend charts, web dashboard, and
multi-language DAP support.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest


# ── helpers ────────────────────────────────────────────────────────────────

def _make_snapshot(seq: int, func: str = "checkout", **locals_: Any):
    """Build a minimal StateSnapshot-like dict for testing."""
    from src.state_tracker.dap_client import StateSnapshot
    return StateSnapshot(
        event="call", thread_id=1, file="checkout.py",
        line=10 + seq, function=func,
        locals=locals_ or {"total": seq * 10.0, "qty": seq},
        sequence=seq,
    )


def _make_flow_trace(flow_id: str = "checkout", run_id: str = "run001",
                     n_snapshots: int = 4):
    from src.state_tracker.trace_recorder import FlowTrace
    snaps = [_make_snapshot(i) for i in range(n_snapshots)]
    return FlowTrace(flow_id=flow_id, run_id=run_id, snapshots=snaps)


def _make_trace_delta(flow_id: str = "checkout", run_id: str = "run001"):
    from src.delta_engine.state_diff import TraceDelta, SnapshotDelta, VariableDelta
    change = VariableDelta(
        name="total", change_type="changed",
        old_value=0.0, new_value=10.0,
        old_type="float", new_type="float",
        deep_path="",
    )
    sd = SnapshotDelta(
        from_seq=0, to_seq=1,
        from_location="checkout.py:10",
        to_location="checkout.py:11",
        changes=[change],
    )
    return TraceDelta(flow_id=flow_id, run_id=run_id, deltas=[sd])


# ══════════════════════════════════════════════════════════════════════════
# 1. OTelExporter
# ══════════════════════════════════════════════════════════════════════════

class TestOTelExporter:

    def test_import(self):
        from src.observability import OTelExporter
        assert OTelExporter

    def test_export_returns_resource_spans(self, tmp_path):
        from src.observability import OTelExporter
        trace = _make_flow_trace(n_snapshots=3)
        delta = _make_trace_delta()
        exporter = OTelExporter(endpoint=None, fallback_path=str(tmp_path / "otel"))
        result = exporter.export_trace(trace, delta)
        assert result is not None
        assert len(result.spans) > 0

    def test_fallback_file_written(self, tmp_path):
        from src.observability import OTelExporter
        trace = _make_flow_trace(n_snapshots=2)
        delta = _make_trace_delta()
        otel_dir = tmp_path / "otel"
        exporter = OTelExporter(endpoint=None, fallback_path=str(otel_dir))
        exporter.export_trace(trace, delta)
        files = list(otel_dir.rglob("*.jsonl"))
        assert len(files) > 0
        data = json.loads(files[0].read_text().splitlines()[0])
        assert "resourceSpans" in data

    def test_span_has_trace_id(self, tmp_path):
        from src.observability import OTelExporter
        trace = _make_flow_trace(n_snapshots=2)
        delta = _make_trace_delta()
        exporter = OTelExporter(endpoint=None, fallback_path=str(tmp_path / "otel"))
        result = exporter.export_trace(trace, delta)
        # All spans share the same trace_id
        trace_ids = {s.trace_id for s in result.spans}
        assert len(trace_ids) == 1

    def test_span_attributes_contain_flow_id(self, tmp_path):
        from src.observability import OTelExporter
        trace = _make_flow_trace(flow_id="my_flow", n_snapshots=2)
        delta = _make_trace_delta(flow_id="my_flow")
        exporter = OTelExporter(endpoint=None, fallback_path=str(tmp_path / "otel"))
        result = exporter.export_trace(trace, delta)
        all_keys = [a.key for s in result.spans for a in s.attributes]
        assert any("flow" in k for k in all_keys)

    def test_delta_events_attached(self, tmp_path):
        from src.observability import OTelExporter
        trace = _make_flow_trace(n_snapshots=3)
        delta = _make_trace_delta()  # 1 SnapshotDelta with 1 VariableDelta
        exporter = OTelExporter(endpoint=None, fallback_path=str(tmp_path / "otel"))
        result = exporter.export_trace(trace, delta)
        all_events = [e.name for s in result.spans for e in s.events]
        assert any("variable" in evt.lower() or "change" in evt.lower() or "total" in evt.lower()
                   for evt in all_events)

    def test_empty_trace_does_not_crash(self, tmp_path):
        from src.observability import OTelExporter
        from src.state_tracker.trace_recorder import FlowTrace
        from src.delta_engine.state_diff import TraceDelta
        trace = FlowTrace(flow_id="empty", run_id="r0", snapshots=[])
        delta = TraceDelta(flow_id="empty", run_id="r0", deltas=[])
        exporter = OTelExporter(endpoint=None, fallback_path=str(tmp_path / "otel"))
        result = exporter.export_trace(trace, delta)
        assert result is not None
        assert result.spans == []

    def test_to_dict_structure(self, tmp_path):
        from src.observability import OTelExporter
        trace = _make_flow_trace(n_snapshots=2)
        delta = _make_trace_delta()
        exporter = OTelExporter(endpoint=None, fallback_path=str(tmp_path / "otel"))
        result = exporter.export_trace(trace, delta)
        for s in result.spans:
            d = s.to_dict()
            assert "traceId" in d
            assert "spanId" in d
            assert "name" in d


# ══════════════════════════════════════════════════════════════════════════
# 2. TrendChartGenerator
# ══════════════════════════════════════════════════════════════════════════

class _FakeDeltaStore:
    """Mimics DeltaStore for trend-chart and dashboard tests."""

    store_path = ".flowdelta/test"

    def __init__(self, runs: List[Dict], deltas: Optional[Dict] = None):
        self._runs = runs
        self._deltas = deltas or {}

    def list_runs(self, flow_id: str = None):
        if flow_id:
            return [r for r in self._runs if r.get("flow_id") == flow_id]
        return self._runs

    def load_delta(self, run_id: str):
        return self._deltas.get(run_id)

    def load_trace(self, run_id: str):
        return None


class TestTrendChartGenerator:

    def _make_store(self):
        runs = [
            {"run_id": "r1", "flow_id": "checkout", "saved_at": "2024-01-01T10:00:00Z", "golden": True},
            {"run_id": "r2", "flow_id": "checkout", "saved_at": "2024-01-02T10:00:00Z", "golden": False},
            {"run_id": "r3", "flow_id": "checkout", "saved_at": "2024-01-03T10:00:00Z", "golden": False},
        ]
        deltas = {
            "r1": {"deltas": [{"changes": [1, 2]}, {"changes": [3]}]},
            "r2": {"deltas": [{"changes": [1]}]},
            "r3": {"deltas": [{"changes": [1, 2, 3, 4]}]},
        }
        return _FakeDeltaStore(runs, deltas)

    def test_import(self):
        from src.observability import TrendChartGenerator
        assert TrendChartGenerator

    def test_get_points_returns_list(self):
        from src.observability import TrendChartGenerator
        store = self._make_store()
        gen = TrendChartGenerator(store)
        pts = gen.get_points("checkout")
        assert len(pts) == 3

    def test_points_sorted_by_saved_at(self):
        from src.observability import TrendChartGenerator
        store = self._make_store()
        gen = TrendChartGenerator(store)
        pts = gen.get_points("checkout")
        assert [p.run_id for p in pts] == ["r1", "r2", "r3"]

    def test_total_changes_summed(self):
        from src.observability import TrendChartGenerator
        store = self._make_store()
        gen = TrendChartGenerator(store)
        pts = gen.get_points("checkout")
        # r1: 2+1=3 items as changes lists; r2: 1; r3: 4
        assert pts[0].total_changes == 3
        assert pts[1].total_changes == 1
        assert pts[2].total_changes == 4

    def test_golden_flag_preserved(self):
        from src.observability import TrendChartGenerator
        store = self._make_store()
        gen = TrendChartGenerator(store)
        pts = gen.get_points("checkout")
        assert pts[0].golden is True
        assert pts[1].golden is False

    def test_empty_flow_gives_empty_list(self):
        from src.observability import TrendChartGenerator
        store = _FakeDeltaStore([])
        gen = TrendChartGenerator(store)
        assert gen.get_points("unknown") == []

    def test_to_json_is_valid_json(self):
        from src.observability import TrendChartGenerator
        store = self._make_store()
        gen = TrendChartGenerator(store)
        payload = gen.to_json("checkout")
        parsed = json.loads(payload)
        assert "flow_id" in parsed
        assert "points" in parsed

    def test_write_html_creates_file(self, tmp_path):
        from src.observability import TrendChartGenerator
        store = self._make_store()
        gen = TrendChartGenerator(store)
        out = tmp_path / "trend.html"
        gen.write_html("checkout", str(out))
        assert out.exists()
        content = out.read_text()
        assert "chart.js" in content.lower() or "Chart" in content

    def test_print_ascii_no_crash(self, capsys):
        from src.observability import TrendChartGenerator
        store = self._make_store()
        gen = TrendChartGenerator(store)
        gen.print_ascii("checkout")
        captured = capsys.readouterr()
        assert "checkout" in captured.out.lower() or len(captured.out) > 0

    def test_trend_point_to_dict(self):
        from src.observability.trend_chart import TrendPoint
        pt = TrendPoint(
            run_id="r1", saved_at="2024-01-01T00:00:00Z",
            sequence=1, total_changes=5, golden=True,
        )
        d = pt.to_dict()
        assert d["run_id"] == "r1"
        assert d["total_changes"] == 5
        assert d["golden"] is True


# ══════════════════════════════════════════════════════════════════════════
# 3. DeltaDashboard (FastAPI)
# ══════════════════════════════════════════════════════════════════════════

class TestDeltaDashboard:

    def _make_store(self):
        runs = [
            {"run_id": "r1", "flow_id": "checkout", "saved_at": "2024-01-01T10:00:00Z", "golden": True},
            {"run_id": "r2", "flow_id": "payment",  "saved_at": "2024-01-02T10:00:00Z", "golden": False},
        ]
        deltas = {
            "r1": {"deltas": [{"changes": [{"name": "total", "change_type": "changed",
                                             "old_value": 0, "new_value": 10}]}],
                   "flow_id": "checkout", "run_id": "r1"},
        }
        return _FakeDeltaStore(runs, deltas)

    def _make_app(self):
        from src.observability import DeltaDashboard
        store = self._make_store()
        dash = DeltaDashboard(store)
        return dash.get_app()

    def test_import(self):
        pytest.importorskip("fastapi")
        from src.observability import DeltaDashboard
        assert DeltaDashboard

    def test_health_endpoint(self):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient
        app = self._make_app()
        client = TestClient(app)
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_flows_endpoint(self):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient
        app = self._make_app()
        client = TestClient(app)
        resp = client.get("/api/flows")
        assert resp.status_code == 200
        data = resp.json()
        assert "flows" in data
        assert isinstance(data["flows"], list)
        assert len(data["flows"]) >= 1

    def test_runs_endpoint(self):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient
        app = self._make_app()
        client = TestClient(app)
        resp = client.get("/api/flows/checkout/runs")
        assert resp.status_code == 200
        data = resp.json()
        assert "runs" in data or isinstance(data, list)

    def test_delta_endpoint_found(self):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient
        app = self._make_app()
        client = TestClient(app)
        resp = client.get("/api/runs/r1/delta")
        assert resp.status_code == 200

    def test_delta_endpoint_not_found(self):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient
        app = self._make_app()
        client = TestClient(app)
        resp = client.get("/api/runs/nonexistent/delta")
        assert resp.status_code == 404

    def test_trend_endpoint(self):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient
        app = self._make_app()
        client = TestClient(app)
        resp = client.get("/api/flows/checkout/trend")
        assert resp.status_code == 200
        data = resp.json()
        assert "points" in data or isinstance(data, list)

    def test_root_returns_html(self):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient
        app = self._make_app()
        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "html" in resp.headers.get("content-type", "").lower() or \
               "<!DOCTYPE" in resp.text or "<html" in resp.text


# ══════════════════════════════════════════════════════════════════════════
# 4. JavaDAPLauncher & CSharpDAPLauncher
# ══════════════════════════════════════════════════════════════════════════

class TestJavaDAPLauncher:

    def test_import(self):
        from src.multi_lang import JavaDAPLauncher
        assert JavaDAPLauncher

    def test_normalize_variable_int(self):
        from src.multi_lang import JavaDAPLauncher
        raw = {"variables": [{"name": "count", "type": "int", "value": "42"}]}
        result = JavaDAPLauncher.normalize_variable(raw)
        assert result["count"] == 42

    def test_normalize_variable_float(self):
        from src.multi_lang import JavaDAPLauncher
        raw = {"variables": [{"name": "price", "type": "double", "value": "9.99"}]}
        result = JavaDAPLauncher.normalize_variable(raw)
        assert abs(result["price"] - 9.99) < 0.001

    def test_normalize_variable_boolean_true(self):
        from src.multi_lang import JavaDAPLauncher
        raw = {"variables": [{"name": "active", "type": "boolean", "value": "true"}]}
        result = JavaDAPLauncher.normalize_variable(raw)
        assert result["active"] is True

    def test_normalize_variable_boolean_false(self):
        from src.multi_lang import JavaDAPLauncher
        raw = {"variables": [{"name": "active", "type": "boolean", "value": "false"}]}
        result = JavaDAPLauncher.normalize_variable(raw)
        assert result["active"] is False

    def test_normalize_variable_null(self):
        from src.multi_lang import JavaDAPLauncher
        raw = {"variables": [{"name": "user", "type": "Object", "value": "null"}]}
        result = JavaDAPLauncher.normalize_variable(raw)
        assert result["user"] is None

    def test_normalize_variable_string(self):
        from src.multi_lang import JavaDAPLauncher
        raw = {"variables": [{"name": "name", "type": "String", "value": '"Alice"'}]}
        result = JavaDAPLauncher.normalize_variable(raw)
        assert result["name"] == "Alice"

    def test_normalize_variable_multiple(self):
        from src.multi_lang import JavaDAPLauncher
        raw = {"variables": [
            {"name": "x", "type": "int", "value": "1"},
            {"name": "y", "type": "int", "value": "2"},
        ]}
        result = JavaDAPLauncher.normalize_variable(raw)
        assert result == {"x": 1, "y": 2}

    def test_normalize_variable_empty(self):
        from src.multi_lang import JavaDAPLauncher
        result = JavaDAPLauncher.normalize_variable({"variables": []})
        assert result == {}

    def test_find_debug_jar_env(self, monkeypatch, tmp_path):
        from src.multi_lang import JavaDAPLauncher
        jar = tmp_path / "com.microsoft.java.debug.plugin-0.50.jar"
        jar.write_bytes(b"fake")
        monkeypatch.setenv("JAVA_DEBUG_JAR", str(jar))
        launcher = JavaDAPLauncher.__new__(JavaDAPLauncher)
        launcher._COMMON_DEBUG_JAR_LOCATIONS = []
        found = launcher._find_debug_jar()
        assert found == jar


class TestCSharpDAPLauncher:

    def test_import(self):
        from src.multi_lang import CSharpDAPLauncher
        assert CSharpDAPLauncher

    def test_normalize_variable_int32(self):
        from src.multi_lang import CSharpDAPLauncher
        raw = {"variables": [{"name": "count", "type": "Int32", "value": "7"}]}
        result = CSharpDAPLauncher.normalize_variable(raw)
        assert result["count"] == 7

    def test_normalize_variable_double(self):
        from src.multi_lang import CSharpDAPLauncher
        raw = {"variables": [{"name": "price", "type": "Double", "value": "3.14"}]}
        result = CSharpDAPLauncher.normalize_variable(raw)
        assert abs(result["price"] - 3.14) < 0.001

    def test_normalize_variable_bool_true(self):
        from src.multi_lang import CSharpDAPLauncher
        raw = {"variables": [{"name": "ok", "type": "bool", "value": "True"}]}
        result = CSharpDAPLauncher.normalize_variable(raw)
        assert result["ok"] is True

    def test_normalize_variable_bool_false(self):
        from src.multi_lang import CSharpDAPLauncher
        raw = {"variables": [{"name": "ok", "type": "bool", "value": "False"}]}
        result = CSharpDAPLauncher.normalize_variable(raw)
        assert result["ok"] is False

    def test_normalize_variable_null(self):
        from src.multi_lang import CSharpDAPLauncher
        raw = {"variables": [{"name": "obj", "type": "Object", "value": "null"}]}
        result = CSharpDAPLauncher.normalize_variable(raw)
        assert result["obj"] is None

    def test_normalize_variable_string(self):
        from src.multi_lang import CSharpDAPLauncher
        raw = {"variables": [{"name": "msg", "type": "string", "value": '"hello"'}]}
        result = CSharpDAPLauncher.normalize_variable(raw)
        assert result["msg"] == "hello"

    def test_find_dll_finds_project_dll(self, tmp_path):
        from src.multi_lang import CSharpDAPLauncher
        # Create a fake project structure
        dll = tmp_path / "bin" / "Debug" / "net8.0" / "MyApp.dll"
        dll.parent.mkdir(parents=True)
        dll.write_bytes(b"fake")
        launcher = CSharpDAPLauncher.__new__(CSharpDAPLauncher)
        launcher.project_path = tmp_path
        launcher.configuration = "Debug"
        found = launcher._find_dll()
        assert found is not None
        assert found.name == "MyApp.dll"

    def test_find_dll_returns_none_when_missing(self, tmp_path):
        from src.multi_lang import CSharpDAPLauncher
        launcher = CSharpDAPLauncher.__new__(CSharpDAPLauncher)
        launcher.project_path = tmp_path
        launcher.configuration = "Debug"
        assert launcher._find_dll() is None


# ══════════════════════════════════════════════════════════════════════════
# 5. JavaASTAnalyzer / CSharpASTAnalyzer
# ══════════════════════════════════════════════════════════════════════════

class TestJavaASTAnalyzer:

    _SAMPLE_JAVA = """\
public class Checkout {
    private double total;

    public void addItem(String name, double price) {
        total += price;
        updateCart(name);
    }

    public void updateCart(String name) {
        System.out.println(name);
    }

    private double getTotal() {
        return total;
    }
}
"""

    def test_import(self):
        from src.multi_lang import JavaASTAnalyzer
        assert JavaASTAnalyzer

    def test_analyze_extracts_methods(self, tmp_path):
        from src.multi_lang import JavaASTAnalyzer
        src = tmp_path / "Checkout.java"
        src.write_text(self._SAMPLE_JAVA)
        result = JavaASTAnalyzer().analyze(src)
        names = [f["name"] for f in result["functions"]]
        assert "addItem" in names
        assert "updateCart" in names

    def test_analyze_identifies_language(self, tmp_path):
        from src.multi_lang import JavaASTAnalyzer
        src = tmp_path / "Checkout.java"
        src.write_text(self._SAMPLE_JAVA)
        result = JavaASTAnalyzer().analyze(src)
        assert result["language"] == "java"

    def test_analyze_finds_calls(self, tmp_path):
        from src.multi_lang import JavaASTAnalyzer
        src = tmp_path / "Checkout.java"
        src.write_text(self._SAMPLE_JAVA)
        result = JavaASTAnalyzer().analyze(src)
        # addItem calls updateCart
        add_item = next(f for f in result["functions"] if f["name"] == "addItem")
        assert "updateCart" in add_item["calls"]

    def test_analyze_empty_file(self, tmp_path):
        from src.multi_lang import JavaASTAnalyzer
        src = tmp_path / "Empty.java"
        src.write_text("")
        result = JavaASTAnalyzer().analyze(src)
        assert result["functions"] == []


class TestCSharpASTAnalyzer:

    _SAMPLE_CS = """\
using System;

public class Checkout {
    private double total;

    public void AddItem(string name, double price) {
        total += price;
        UpdateCart(name);
    }

    private void UpdateCart(string name) {
        Console.WriteLine(name);
    }

    public double GetTotal() {
        return total;
    }
}
"""

    def test_import(self):
        from src.multi_lang import CSharpASTAnalyzer
        assert CSharpASTAnalyzer

    def test_analyze_extracts_methods(self, tmp_path):
        from src.multi_lang import CSharpASTAnalyzer
        src = tmp_path / "Checkout.cs"
        src.write_text(self._SAMPLE_CS)
        result = CSharpASTAnalyzer().analyze(src)
        names = [f["name"] for f in result["functions"]]
        assert "AddItem" in names
        assert "UpdateCart" in names

    def test_analyze_identifies_language(self, tmp_path):
        from src.multi_lang import CSharpASTAnalyzer
        src = tmp_path / "Checkout.cs"
        src.write_text(self._SAMPLE_CS)
        result = CSharpASTAnalyzer().analyze(src)
        assert result["language"] == "csharp"

    def test_analyze_finds_calls(self, tmp_path):
        from src.multi_lang import CSharpASTAnalyzer
        src = tmp_path / "Checkout.cs"
        src.write_text(self._SAMPLE_CS)
        result = CSharpASTAnalyzer().analyze(src)
        add_item = next(f for f in result["functions"] if f["name"] == "AddItem")
        assert "UpdateCart" in add_item["calls"]

    def test_analyze_empty_file(self, tmp_path):
        from src.multi_lang import CSharpASTAnalyzer
        src = tmp_path / "Empty.cs"
        src.write_text("")
        result = CSharpASTAnalyzer().analyze(src)
        assert result["functions"] == []

    def test_analyze_skips_keywords(self, tmp_path):
        from src.multi_lang import CSharpASTAnalyzer
        src = tmp_path / "Checkout.cs"
        src.write_text(self._SAMPLE_CS)
        result = CSharpASTAnalyzer().analyze(src)
        names = [f["name"] for f in result["functions"]]
        # 'if', 'while', etc. must not appear as methods
        for kw in ("if", "while", "for", "foreach", "switch", "using", "catch"):
            assert kw not in names


# ══════════════════════════════════════════════════════════════════════════
# 6. CLI smoke tests (orchestrator)
# ══════════════════════════════════════════════════════════════════════════

class TestSpring4CLICommands:
    """Verify Sprint 4 CLI commands are registered and have correct names."""

    def test_otel_export_registered(self):
        from src.orchestrator import cli
        assert "otel-export" in cli.commands

    def test_trend_registered(self):
        from src.orchestrator import cli
        assert "trend" in cli.commands

    def test_dashboard_registered(self):
        from src.orchestrator import cli
        assert "dashboard" in cli.commands

    def test_dap_polyglot_registered(self):
        from src.orchestrator import cli
        assert "dap-polyglot" in cli.commands

    def test_all_sprint4_commands_present(self):
        from src.orchestrator import cli
        for cmd in ("otel-export", "trend", "dashboard", "dap-polyglot"):
            assert cmd in cli.commands, f"CLI command '{cmd}' is missing"
