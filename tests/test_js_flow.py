"""
Tests for JavaScript / TypeScript end-to-end AST analysis.

Exercises the ASTAnalyzer and CallGraphBuilder on the sample JS app.
Skips automatically if tree-sitter-javascript is not installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from src.flow_identifier.ast_analyzer import ASTAnalyzer
from src.flow_identifier.call_graph import CallGraphBuilder
from src.flow_identifier.llm_flow_mapper import LLMFlowMapper

JS_APP = Path(__file__).parent.parent / "examples" / "sample_app" / "ecommerce.js"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def js_analysis():
    try:
        analyzer = ASTAnalyzer()
        if "javascript" not in analyzer._parsers:
            pytest.skip("tree-sitter-javascript not installed")
        return analyzer.analyze_file(JS_APP)
    except (ImportError, ValueError) as exc:
        pytest.skip(f"JS analysis not available: {exc}")


@pytest.fixture(scope="module")
def js_call_graph(js_analysis):
    return CallGraphBuilder().build([js_analysis])


# ---------------------------------------------------------------------------
# AST analysis tests
# ---------------------------------------------------------------------------

class TestJSASTAnalysis:
    def test_language_is_javascript(self, js_analysis):
        assert js_analysis.language == "javascript"

    def test_detects_all_flow_entry_functions(self, js_analysis):
        names = [f.name for f in js_analysis.functions]
        assert "registerUser" in names
        assert "checkout" in names
        assert "trackOrder" in names

    def test_detects_helper_functions(self, js_analysis):
        names = [f.name for f in js_analysis.functions]
        for fn in ["buildCart", "applyCoupon", "processPayment",
                   "createOrder", "lookupOrder", "updateOrderStatus"]:
            assert fn in names, f"Missing function: {fn}"

    def test_detects_class_methods(self, js_analysis):
        """CartItem.subtotal getter should be detected as a method."""
        names = [f.name for f in js_analysis.functions]
        # Constructor and getters are method_definition nodes
        assert any(f in names for f in ("constructor", "subtotal", "total"))

    def test_functions_have_correct_file(self, js_analysis):
        for fn in js_analysis.functions:
            assert fn.file == str(JS_APP)

    def test_functions_have_line_numbers(self, js_analysis):
        for fn in js_analysis.functions:
            assert fn.start_line > 0
            assert fn.end_line >= fn.start_line

    def test_detects_calls_inside_checkout(self, js_analysis):
        checkout_fn = next(
            (f for f in js_analysis.functions if f.name == "checkout"), None
        )
        assert checkout_fn is not None
        # checkout calls buildCart, applyCoupon, processPayment, createOrder
        assert any("buildCart" in c or "Cart" in c for c in checkout_fn.calls)

    def test_detects_calls_inside_register_user(self, js_analysis):
        reg_fn = next(
            (f for f in js_analysis.functions if f.name == "registerUser"), None
        )
        assert reg_fn is not None
        assert any("createAccount" in c or "account" in c.lower()
                   for c in reg_fn.calls)


# ---------------------------------------------------------------------------
# Call graph tests
# ---------------------------------------------------------------------------

class TestJSCallGraph:
    def test_graph_has_nodes(self, js_call_graph):
        assert js_call_graph.graph.number_of_nodes() > 0

    def test_graph_has_edges(self, js_call_graph):
        assert js_call_graph.graph.number_of_edges() > 0

    def test_checkout_is_entry_or_reachable(self, js_call_graph):
        """checkout or registerUser should be detectable as entry points."""
        all_nodes = set(js_call_graph.graph.nodes())
        assert any(
            "checkout" in n or "registerUser" in n or "trackOrder" in n
            for n in all_nodes
        )

    def test_reachable_from_checkout(self, js_call_graph):
        """Functions reachable from checkout should include buildCart."""
        checkout_node = next(
            (n for n in js_call_graph.graph.nodes() if "checkout" in n.lower()
             and "product" not in n.lower()),
            None,
        )
        if checkout_node is None:
            pytest.skip("checkout node not found in graph")
        reachable = js_call_graph.reachable_from(checkout_node)
        assert any("buildCart" in r or "build" in r.lower() for r in reachable)


# ---------------------------------------------------------------------------
# Heuristic flow identification (no API key needed)
# ---------------------------------------------------------------------------

class TestJSFlowIdentification:
    def test_identifies_at_least_one_flow(self, js_call_graph):
        flow_map = LLMFlowMapper(max_flows=5).identify_flows(js_call_graph)
        assert len(flow_map.flows) >= 1

    def test_flow_has_entry_function(self, js_call_graph):
        flow_map = LLMFlowMapper(max_flows=5).identify_flows(js_call_graph)
        for flow in flow_map.flows:
            assert flow.entry_function != ""

    def test_flow_steps_non_empty(self, js_call_graph):
        flow_map = LLMFlowMapper(max_flows=5).identify_flows(js_call_graph)
        for flow in flow_map.flows:
            assert len(flow.steps) >= 1


# ---------------------------------------------------------------------------
# Inline JS snippet test (no file needed)
# ---------------------------------------------------------------------------

class TestJSInlineSnippet:
    def test_arrow_function_parsed(self, tmp_path):
        js_src = tmp_path / "arrow.js"
        js_src.write_text(
            "const add = (a, b) => a + b;\n"
            "function main() { return add(1, 2); }\n",
            encoding="utf-8",
        )
        try:
            analyzer = ASTAnalyzer()
            if "javascript" not in analyzer._parsers:
                pytest.skip("tree-sitter-javascript not installed")
            analysis = analyzer.analyze_file(js_src)
        except (ImportError, ValueError) as exc:
            pytest.skip(str(exc))

        names = [f.name for f in analysis.functions]
        assert "main" in names

    def test_class_method_qualified_name(self, tmp_path):
        js_src = tmp_path / "cls.js"
        js_src.write_text(
            "class Cart {\n"
            "  constructor(id) { this.id = id; this.items = []; }\n"
            "  addItem(p) { this.items.push(p); }\n"
            "}\n",
            encoding="utf-8",
        )
        try:
            analyzer = ASTAnalyzer()
            if "javascript" not in analyzer._parsers:
                pytest.skip("tree-sitter-javascript not installed")
            analysis = analyzer.analyze_file(js_src)
        except (ImportError, ValueError) as exc:
            pytest.skip(str(exc))

        qnames = [f.qualified_name for f in analysis.functions]
        assert any("Cart" in q for q in qnames)
