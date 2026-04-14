"""
AST Analyzer – Phase 1 of FlowDelta.

Uses tree-sitter to parse Python (and JavaScript) source files and extract:
  - All function/method definitions with their source ranges
  - Intra-function call edges
  - Module-level entry calls
  - Import statements

These raw facts feed the CallGraph builder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# tree-sitter >= 0.21 API
from tree_sitter import Language, Node, Parser

try:
    import tree_sitter_python as tspython
    _PY_LANGUAGE = Language(tspython.language())
except ImportError:
    _PY_LANGUAGE = None

try:
    import tree_sitter_javascript as tsjs
    _JS_LANGUAGE = Language(tsjs.language())
except ImportError:
    _JS_LANGUAGE = None


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FunctionDef:
    """Represents a single function or method definition."""
    name: str
    qualified_name: str            # ClassName.method_name or function_name
    file: str
    start_line: int
    end_line: int
    args: List[str] = field(default_factory=list)
    calls: List[str] = field(default_factory=list)   # names of functions called
    decorators: List[str] = field(default_factory=list)
    class_name: Optional[str] = None
    is_async: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "qualified_name": self.qualified_name,
            "file": self.file,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "args": self.args,
            "calls": self.calls,
            "decorators": self.decorators,
            "class_name": self.class_name,
            "is_async": self.is_async,
        }


@dataclass
class ASTAnalysis:
    """Complete analysis result for one source file."""
    file: str
    language: str
    functions: List[FunctionDef] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    entry_calls: List[str] = field(default_factory=list)   # module-level calls

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "language": self.language,
            "functions": [f.to_dict() for f in self.functions],
            "imports": self.imports,
            "entry_calls": self.entry_calls,
        }


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

class ASTAnalyzer:
    """
    Parses source files and extracts structural information needed to build
    a call graph and identify flows.

    Usage::

        analyzer = ASTAnalyzer()
        analysis = analyzer.analyze_file("src/checkout.py")
        print(json.dumps(analysis.to_dict(), indent=2))
    """

    _LANG_MAP = {
        ".py": "python",
        ".pyw": "python",
        ".js": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
    }

    def __init__(self) -> None:
        self._parsers: Dict[str, Parser] = {}
        if _PY_LANGUAGE:
            self._parsers["python"] = Parser(_PY_LANGUAGE)
        if _JS_LANGUAGE:
            self._parsers["javascript"] = Parser(_JS_LANGUAGE)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze_file(self, filepath: str | Path) -> ASTAnalysis:
        """Parse *filepath* and return an :class:`ASTAnalysis`."""
        path = Path(filepath)
        lang = self._LANG_MAP.get(path.suffix.lower())
        if lang is None:
            raise ValueError(f"Unsupported file extension: {path.suffix}")
        if lang not in self._parsers:
            raise RuntimeError(
                f"tree-sitter grammar for '{lang}' not installed. "
                f"Run: pip install tree-sitter-{lang}"
            )

        source = path.read_text(encoding="utf-8")
        parser = self._parsers[lang]
        tree = parser.parse(source.encode("utf-8"))

        if lang == "python":
            return self._analyze_python(tree.root_node, source, str(path))
        return self._analyze_javascript(tree.root_node, source, str(path))

    def analyze_directory(
        self,
        directory: str | Path,
        recursive: bool = True,
    ) -> List[ASTAnalysis]:
        """Analyze all supported source files under *directory*."""
        root = Path(directory)
        pattern = "**/*" if recursive else "*"
        results: List[ASTAnalysis] = []
        for p in root.glob(pattern):
            if p.suffix.lower() in self._LANG_MAP and p.is_file():
                try:
                    results.append(self.analyze_file(p))
                except Exception as exc:  # noqa: BLE001
                    print(f"[ASTAnalyzer] Skipping {p}: {exc}")
        return results

    # ------------------------------------------------------------------
    # Python analysis
    # ------------------------------------------------------------------

    def _analyze_python(self, root: Node, source: str, filepath: str) -> ASTAnalysis:
        analysis = ASTAnalysis(file=filepath, language="python")
        src_bytes = source.encode("utf-8")
        self._walk_python(root, src_bytes, analysis, class_name=None, depth=0)
        return analysis

    def _walk_python(
        self,
        node: Node,
        src: bytes,
        analysis: ASTAnalysis,
        class_name: Optional[str],
        depth: int,
    ) -> None:
        if node.type in ("function_definition", "async_function_definition"):
            func = self._extract_python_function(node, src, class_name, analysis.file)
            analysis.functions.append(func)
            return  # children are handled inside extractor

        if node.type == "class_definition":
            name_node = self._child_by_field(node, "name")
            cname = src[name_node.start_byte:name_node.end_byte].decode() if name_node else "Unknown"
            body = self._child_by_field(node, "body")
            if body:
                for child in body.children:
                    self._walk_python(child, src, analysis, class_name=cname, depth=depth + 1)
            return

        if node.type in ("import_statement", "import_from_statement"):
            analysis.imports.append(src[node.start_byte:node.end_byte].decode())
            return

        # Module-level call (entry point)
        if depth == 0 and node.type == "expression_statement":
            for child in node.children:
                if child.type == "call":
                    analysis.entry_calls.append(self._call_name(child, src))

        for child in node.children:
            self._walk_python(child, src, analysis, class_name, depth)

    def _extract_python_function(
        self,
        node: Node,
        src: bytes,
        class_name: Optional[str],
        filepath: str,
    ) -> FunctionDef:
        is_async = node.type == "async_function_definition"
        name_node = self._child_by_field(node, "name")
        name = src[name_node.start_byte:name_node.end_byte].decode() if name_node else "<anon>"
        qualified = f"{class_name}.{name}" if class_name else name

        # Parameters
        params_node = self._child_by_field(node, "parameters")
        args = self._extract_python_params(params_node, src) if params_node else []

        # Decorators: previous siblings of type 'decorator'
        decorators = [
            src[sib.start_byte:sib.end_byte].decode()
            for sib in (node.prev_sibling,) if sib and sib.type == "decorator"
        ]

        # Calls inside function body
        body = self._child_by_field(node, "body")
        calls: List[str] = []
        if body:
            self._collect_calls_python(body, src, calls)

        return FunctionDef(
            name=name,
            qualified_name=qualified,
            file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            args=args,
            calls=list(dict.fromkeys(calls)),   # deduplicate, preserve order
            decorators=decorators,
            class_name=class_name,
            is_async=is_async,
        )

    def _extract_python_params(self, node: Node, src: bytes) -> List[str]:
        params: List[str] = []
        for child in node.children:
            if child.type in ("identifier", "typed_parameter", "default_parameter",
                              "typed_default_parameter"):
                ident = child if child.type == "identifier" else self._first_child_of_type(child, "identifier")
                if ident:
                    name = src[ident.start_byte:ident.end_byte].decode()
                    if name not in ("self", "cls"):
                        params.append(name)
        return params

    def _collect_calls_python(self, node: Node, src: bytes, calls: List[str]) -> None:
        if node.type == "call":
            name = self._call_name(node, src)
            if name:
                calls.append(name)
        for child in node.children:
            self._collect_calls_python(child, src, calls)

    def _call_name(self, node: Node, src: bytes) -> str:
        """Extract the callee name from a 'call' node."""
        fn_node = node.child_by_field_name("function") or (node.children[0] if node.children else None)
        if fn_node is None:
            return ""
        if fn_node.type == "identifier":
            return src[fn_node.start_byte:fn_node.end_byte].decode()
        if fn_node.type == "attribute":
            attr = fn_node.child_by_field_name("attribute")
            obj = fn_node.child_by_field_name("object")
            if attr:
                obj_name = src[obj.start_byte:obj.end_byte].decode() if obj else ""
                attr_name = src[attr.start_byte:attr.end_byte].decode()
                return f"{obj_name}.{attr_name}"
        return src[fn_node.start_byte:fn_node.end_byte].decode()

    # ------------------------------------------------------------------
    # JavaScript analysis (simplified mirror of Python analysis)
    # ------------------------------------------------------------------

    def _analyze_javascript(self, root: Node, source: str, filepath: str) -> ASTAnalysis:
        analysis = ASTAnalysis(file=filepath, language="javascript")
        src_bytes = source.encode("utf-8")
        self._walk_js(root, src_bytes, analysis, class_name=None, depth=0)
        return analysis

    def _walk_js(
        self,
        node: Node,
        src: bytes,
        analysis: ASTAnalysis,
        class_name: Optional[str],
        depth: int,
    ) -> None:
        if node.type in ("function_declaration", "method_definition",
                          "arrow_function", "function_expression"):
            func = self._extract_js_function(node, src, class_name, analysis.file)
            if func:
                analysis.functions.append(func)
            return

        if node.type == "class_declaration":
            name_node = node.child_by_field_name("name")
            cname = src[name_node.start_byte:name_node.end_byte].decode() if name_node else "Unknown"
            body = node.child_by_field_name("body")
            if body:
                for child in body.children:
                    self._walk_js(child, src, analysis, class_name=cname, depth=depth + 1)
            return

        if node.type == "import_statement":
            analysis.imports.append(src[node.start_byte:node.end_byte].decode())
            return

        for child in node.children:
            self._walk_js(child, src, analysis, class_name, depth)

    def _extract_js_function(
        self,
        node: Node,
        src: bytes,
        class_name: Optional[str],
        filepath: str,
    ) -> Optional[FunctionDef]:
        name_node = node.child_by_field_name("name")
        name = src[name_node.start_byte:name_node.end_byte].decode() if name_node else "<anonymous>"
        qualified = f"{class_name}.{name}" if class_name else name

        params_node = node.child_by_field_name("parameters") or node.child_by_field_name("parameter")
        args: List[str] = []
        if params_node:
            for child in params_node.children:
                if child.type == "identifier":
                    args.append(src[child.start_byte:child.end_byte].decode())

        body = node.child_by_field_name("body")
        calls: List[str] = []
        if body:
            self._collect_calls_js(body, src, calls)

        return FunctionDef(
            name=name,
            qualified_name=qualified,
            file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            args=args,
            calls=list(dict.fromkeys(calls)),
            class_name=class_name,
        )

    def _collect_calls_js(self, node: Node, src: bytes, calls: List[str]) -> None:
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn:
                calls.append(src[fn.start_byte:fn.end_byte].decode())
        for child in node.children:
            self._collect_calls_js(child, src, calls)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _child_by_field(node: Node, field_name: str) -> Optional[Node]:
        return node.child_by_field_name(field_name)

    @staticmethod
    def _first_child_of_type(node: Node, node_type: str) -> Optional[Node]:
        for child in node.children:
            if child.type == node_type:
                return child
        return None
