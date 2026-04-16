"""
LLM Flow Mapper – Phase 1 of FlowDelta.

Sends the call graph summary to an LLM and asks it to cluster functions
into semantically meaningful *application flows* (e.g., "user-login",
"checkout", "data-import").

Each **Flow** has:
  - A human-readable name + description
  - A root entry function
  - The ordered list of functions that make up the happy path
  - Key branch points (conditionals that produce distinct sub-flows)

The mapper also suggests *breakpoint locations* – the functions where
state capture will yield the most signal for delta analysis.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .call_graph import CallGraph


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FlowStep:
    function: str          # qualified_name
    description: str = ""
    is_branch_point: bool = False
    branches: List[str] = field(default_factory=list)   # branch labels


@dataclass
class Flow:
    id: str                           # slug, e.g. "user-login"
    name: str                         # human label
    description: str
    entry_function: str               # where the flow starts
    steps: List[FlowStep] = field(default_factory=list)
    suggested_breakpoints: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "entry_function": self.entry_function,
            "steps": [
                {
                    "function": s.function,
                    "description": s.description,
                    "is_branch_point": s.is_branch_point,
                    "branches": s.branches,
                }
                for s in self.steps
            ],
            "suggested_breakpoints": self.suggested_breakpoints,
            "tags": self.tags,
        }


@dataclass
class FlowMap:
    flows: List[Flow] = field(default_factory=list)
    raw_llm_response: str = ""

    def to_dict(self) -> dict:
        return {
            "flows": [f.to_dict() for f in self.flows],
        }

    def get_flow(self, flow_id: str) -> Optional[Flow]:
        return next((f for f in self.flows if f.id == flow_id), None)


# ---------------------------------------------------------------------------
# Mapper
# ---------------------------------------------------------------------------

class LLMFlowMapper:
    """
    Uses an LLM to identify high-level application flows from a call graph.

    Parameters
    ----------
    model : str
        OpenAI model name (default: ``gpt-4o``).
    api_key : str | None
        OpenAI API key.  Falls back to ``OPENAI_API_KEY`` env var.
    max_flows : int
        Maximum number of flows to request from the LLM.
    """

    _SYSTEM_PROMPT = """\
You are an expert software architect analyzing a call graph to identify distinct \
application flows. A "flow" is a named end-to-end sequence of function calls that \
represents a logical user action or system process (e.g., "user-registration", \
"order-checkout", "file-import").

You will receive a JSON call graph with nodes (functions) and directed edges \
(caller → callee). Identify up to {max_flows} distinct flows.

Return ONLY a JSON object with this schema (no markdown, no explanation):
{{
  "flows": [
    {{
      "id": "<slug>",
      "name": "<Human Readable Name>",
      "description": "<one sentence description>",
      "entry_function": "<qualified_function_name>",
      "steps": [
        {{
          "function": "<qualified_function_name>",
          "description": "<what this step does>",
          "is_branch_point": false,
          "branches": []
        }}
      ],
      "suggested_breakpoints": ["<fn1>", "<fn2>"],
      "tags": ["auth", "write"]
    }}
  ]
}}
""".strip()

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        max_flows: int = 20,
    ) -> None:
        self.model = model
        self.max_flows = max_flows
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._base_url = base_url

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def identify_flows(self, call_graph: CallGraph) -> FlowMap:
        """
        Ask the LLM to identify flows in *call_graph*.

        Falls back to a heuristic-only analysis if no API key is set.
        """
        if not self._api_key:
            return self._heuristic_flows(call_graph)

        graph_summary = self._graph_to_prompt(call_graph)
        raw = self._call_llm(graph_summary)
        return self._parse_response(raw)

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _graph_to_prompt(self, cg: CallGraph) -> str:
        """Produce a concise JSON representation for the prompt."""
        # Limit to internal nodes + their edges to stay within token budget
        internal = [
            n for n, d in cg.graph.nodes(data=True) if not d.get("external")
        ]
        edges = [
            {"from": u, "to": v}
            for u, v in cg.graph.edges()
            if u in internal
        ]
        summary = {
            "entry_points": cg.entry_points,
            "functions": [
                {
                    "name": cg.functions[n].qualified_name,
                    "file": cg.functions[n].file,
                    "line": cg.functions[n].start_line,
                    "args": cg.functions[n].args,
                    "decorators": cg.functions[n].decorators,
                }
                for n in internal
                if n in cg.functions
            ],
            "edges": edges,
        }
        return json.dumps(summary, indent=2)

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    def _call_llm(self, graph_json: str) -> str:
        from ..llm_utils import call_llm
        system = self._SYSTEM_PROMPT.format(max_flows=self.max_flows)
        return call_llm(
            system_prompt=system,
            user_content=graph_json,
            model=self.model,
            api_key=self._api_key,
            base_url=self._base_url,
        )

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, raw: str) -> FlowMap:
        try:
            data: Dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            # Try extracting JSON block from markdown fences
            import re
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
            data = json.loads(m.group(1)) if m else {"flows": []}

        flows: List[Flow] = []
        for f in data.get("flows", []):
            steps = [
                FlowStep(
                    function=s.get("function", ""),
                    description=s.get("description", ""),
                    is_branch_point=s.get("is_branch_point", False),
                    branches=s.get("branches", []),
                )
                for s in f.get("steps", [])
            ]
            flows.append(
                Flow(
                    id=f.get("id", "unknown"),
                    name=f.get("name", "Unknown Flow"),
                    description=f.get("description", ""),
                    entry_function=f.get("entry_function", ""),
                    steps=steps,
                    suggested_breakpoints=f.get("suggested_breakpoints", []),
                    tags=f.get("tags", []),
                )
            )
        return FlowMap(flows=flows, raw_llm_response=raw)

    # ------------------------------------------------------------------
    # Heuristic fallback (no API key required)
    # ------------------------------------------------------------------

    def _heuristic_flows(self, cg: CallGraph) -> FlowMap:
        """
        Simple heuristic: each entry-point function becomes its own flow,
        with all reachable functions as steps.
        """
        flows: List[Flow] = []
        for entry in cg.entry_points[:self.max_flows]:
            reachable = sorted(cg.reachable_from(entry))
            steps = [FlowStep(function=fn) for fn in reachable]
            # Heuristic breakpoints: functions at the boundary of the flow
            bps = reachable[:5]
            slug = entry.lower().replace(".", "-").replace("_", "-")
            flows.append(
                Flow(
                    id=slug,
                    name=entry,
                    description=f"Flow starting from {entry} (heuristic)",
                    entry_function=entry,
                    steps=steps,
                    suggested_breakpoints=bps,
                )
            )
        return FlowMap(flows=flows)
