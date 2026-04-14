"""
Call Graph Builder – Phase 1 of FlowDelta.

Constructs a directed call graph from :class:`ASTAnalysis` results using
NetworkX.  Provides helpers to identify:
  - Entry-point nodes (in-degree == 0)
  - Strongly-connected components (recursive clusters)
  - Reachability sets (all functions reachable from an entry point)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

import networkx as nx

from .ast_analyzer import ASTAnalysis, FunctionDef


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CallEdge:
    caller: str    # qualified_name
    callee: str    # qualified_name (may be unresolved)
    file: str
    line: int


@dataclass
class CallGraph:
    """
    Directed graph where nodes are qualified function names and edges
    represent "caller → callee" relationships.
    """
    graph: nx.DiGraph = field(default_factory=nx.DiGraph)
    functions: Dict[str, FunctionDef] = field(default_factory=dict)
    entry_points: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def reachable_from(self, start: str) -> Set[str]:
        """All nodes reachable from *start* (BFS)."""
        if start not in self.graph:
            return set()
        return nx.descendants(self.graph, start) | {start}

    def callers_of(self, node: str) -> List[str]:
        return list(self.graph.predecessors(node))

    def callees_of(self, node: str) -> List[str]:
        return list(self.graph.successors(node))

    def subgraph_for_flow(self, root: str) -> nx.DiGraph:
        """Return the induced subgraph reachable from *root*."""
        nodes = self.reachable_from(root)
        return self.graph.subgraph(nodes).copy()

    def to_dict(self) -> dict:
        return {
            "nodes": list(self.graph.nodes()),
            "edges": [
                {"from": u, "to": v, **self.graph.edges[u, v]}
                for u, v in self.graph.edges()
            ],
            "entry_points": self.entry_points,
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class CallGraphBuilder:
    """
    Builds a :class:`CallGraph` from one or more :class:`ASTAnalysis` objects.

    Resolution strategy
    -------------------
    For each call recorded in a ``FunctionDef``, we try (in order):

    1. Exact match on ``qualified_name``   (ClassName.method)
    2. Exact match on bare ``name``         (function)
    3. Suffix match on attribute calls      (obj.method → *.method)

    Unresolved calls are added as external nodes with an ``external=True``
    attribute so downstream consumers can filter them if desired.
    """

    def build(self, analyses: List[ASTAnalysis]) -> CallGraph:
        cg = CallGraph()

        # Phase 1: register all known functions
        for analysis in analyses:
            for func in analysis.functions:
                cg.functions[func.qualified_name] = func
                cg.graph.add_node(
                    func.qualified_name,
                    file=func.file,
                    start_line=func.start_line,
                    end_line=func.end_line,
                    external=False,
                )

        # Phase 2: add call edges
        for analysis in analyses:
            for func in analysis.functions:
                for callee_raw in func.calls:
                    callee = self._resolve(callee_raw, cg)
                    if not cg.graph.has_node(callee):
                        cg.graph.add_node(callee, external=True)
                    cg.graph.add_edge(
                        func.qualified_name,
                        callee,
                        file=func.file,
                        line=func.start_line,
                    )

        # Phase 3: identify entry points
        #   - Functions explicitly called at module level
        #   - OR in-degree == 0 among non-external nodes
        module_entries: Set[str] = set()
        for analysis in analyses:
            for raw in analysis.entry_calls:
                resolved = self._resolve(raw, cg)
                module_entries.add(resolved)

        internal_nodes = [n for n, d in cg.graph.nodes(data=True) if not d.get("external")]
        zero_in = {n for n in internal_nodes if cg.graph.in_degree(n) == 0}
        cg.entry_points = sorted(module_entries | zero_in)

        return cg

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def _resolve(self, raw: str, cg: CallGraph) -> str:
        # 1. Exact qualified match
        if raw in cg.functions:
            return raw

        # 2. Bare name match (last segment)
        for qname in cg.functions:
            if qname.split(".")[-1] == raw or qname == raw:
                return qname

        # 3. Attribute call: strip object prefix, match on method name
        if "." in raw:
            method = raw.split(".")[-1]
            candidates = [q for q in cg.functions if q.split(".")[-1] == method]
            if len(candidates) == 1:
                return candidates[0]

        # Unresolved → keep raw name (external)
        return raw
