"""
Tests for the AST Analyzer.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from src.flow_identifier import ASTAnalyzer


SAMPLE_PY = textwrap.dedent("""\
    import os

    class Cart:
        def __init__(self, user_id):
            self.user_id = user_id
            self.items = []

        def add_item(self, product, qty):
            self.items.append((product, qty))

    def checkout(user_id, products):
        cart = Cart(user_id)
        for p, q in products:
            cart.add_item(p, q)
        return cart

    if __name__ == "__main__":
        checkout("u1", [("p1", 2)])
""")


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    p = tmp_path / "app.py"
    p.write_text(SAMPLE_PY, encoding="utf-8")
    return p


class TestASTAnalyzer:
    def test_detects_functions(self, sample_file):
        try:
            analyzer = ASTAnalyzer()
        except Exception:
            pytest.skip("tree-sitter-python not installed")
        analysis = analyzer.analyze_file(sample_file)
        names = [f.name for f in analysis.functions]
        assert "checkout" in names
        assert "__init__" in names
        assert "add_item" in names

    def test_detects_class_methods(self, sample_file):
        try:
            analyzer = ASTAnalyzer()
        except Exception:
            pytest.skip("tree-sitter-python not installed")
        analysis = analyzer.analyze_file(sample_file)
        method = next(f for f in analysis.functions if f.name == "add_item")
        assert method.class_name == "Cart"

    def test_detects_imports(self, sample_file):
        try:
            analyzer = ASTAnalyzer()
        except Exception:
            pytest.skip("tree-sitter-python not installed")
        analysis = analyzer.analyze_file(sample_file)
        assert any("import os" in imp for imp in analysis.imports)

    def test_detects_calls_inside_function(self, sample_file):
        try:
            analyzer = ASTAnalyzer()
        except Exception:
            pytest.skip("tree-sitter-python not installed")
        analysis = analyzer.analyze_file(sample_file)
        checkout_fn = next(f for f in analysis.functions if f.name == "checkout")
        assert any("add_item" in c or "Cart" in c for c in checkout_fn.calls)
