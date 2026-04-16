"""
LLM Test Writer – Phase 4 of FlowDelta.

Uses an LLM to augment generated test specs with:
  - Human-readable test function names
  - Docstrings explaining each test's intent
  - Additional edge-case assertions inferred from the flow semantics
  - Setup/teardown scaffolding

This is purely additive — the raw assertions from :class:`AssertionGenerator`
are always included, and the LLM only enriches them.
"""

from __future__ import annotations

import json
import logging
import os
from typing import List, Optional

from .assertion_gen import AssertionGroup, TestSpec

logger = logging.getLogger(__name__)


class LLMTestWriter:
    """
    Uses an LLM to produce final test function names and docstrings for a
    :class:`TestSpec`.

    Enhances each :class:`AssertionGroup` with:
    - ``test_name`` attribute
    - ``docstring`` attribute
    - Additional LLM-suggested assertion strings

    Parameters
    ----------
    model : str
        OpenAI model name.
    api_key : str | None
        Falls back to ``OPENAI_API_KEY`` env var.
    """

    _SYSTEM_PROMPT = """\
You are a test engineer reviewing automatically generated test cases for 
a software flow. Given a list of assertion groups (each representing a state 
transition in the flow), produce for each group:
  1. A concise snake_case test function name (without "test_" prefix)
  2. A one-sentence docstring explaining what is being validated
  3. Up to 2 additional pytest assertion lines that would strengthen the tests

Return ONLY valid JSON with this schema (no markdown, no explanation):
{
  "tests": [
    {
      "from_location": "<string matching input>",
      "to_location": "<string matching input>",
      "name": "balance_decreases_after_payment",
      "docstring": "Verifies that account balance is correctly debited after payment.",
      "extra_assertions": ["assert result['status'] == 'success'"]
    }
  ]
}
""".strip()

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        self.model = model
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._base_url = base_url

    def augment(self, spec: TestSpec) -> TestSpec:
        """
        Return a new :class:`TestSpec` with LLM-generated metadata attached.
        Falls back gracefully if no API key is configured or the call fails.
        """
        if not self._api_key:
            self._heuristic_names(spec)
            return spec

        prompt = self._build_prompt(spec)
        try:
            raw = self._call_llm(prompt)
        except Exception as exc:  # network error, SSL, quota, etc.
            logger.warning("LLM augmentation failed (%s): falling back to heuristic names", exc)
            self._heuristic_names(spec)
            return spec
        self._apply_response(raw, spec)
        return spec

    # ------------------------------------------------------------------
    # Prompt
    # ------------------------------------------------------------------

    def _build_prompt(self, spec: TestSpec) -> str:
        data = {
            "flow_id": spec.flow_id,
            "groups": [
                {
                    "from_location": g.from_location,
                    "to_location": g.to_location,
                    "assertions": [a.code for a in g.assertions],
                }
                for g in spec.groups
            ],
        }
        return json.dumps(data, indent=2)

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------

    def _call_llm(self, prompt: str) -> str:
        from ..llm_utils import call_llm
        return call_llm(
            system_prompt=self._SYSTEM_PROMPT,
            user_content=prompt,
            model=self.model,
            api_key=self._api_key,
            base_url=self._base_url,
        )

    # ------------------------------------------------------------------
    # Response application
    # ------------------------------------------------------------------

    def _apply_response(self, raw: str, spec: TestSpec) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            import re
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m:
                return
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return

        group_map = {g.from_location: g for g in spec.groups}
        for t in data.get("tests", []):
            grp = group_map.get(t.get("from_location"))
            if grp:
                grp.test_name = t.get("name", "")        # type: ignore[attr-defined]
                grp.docstring = t.get("docstring", "")   # type: ignore[attr-defined]
                grp.extra_assertions = t.get("extra_assertions", [])  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Heuristic fallback
    # ------------------------------------------------------------------

    @staticmethod
    def _heuristic_names(spec: TestSpec) -> None:
        for i, grp in enumerate(spec.groups):
            fn = grp.to_location.split("(")[-1].rstrip(")")
            grp.test_name = f"state_at_{fn.replace('.', '_')}_{i}"  # type: ignore[attr-defined]
            grp.docstring = f"Validates state transition to {grp.to_location}."  # type: ignore[attr-defined]
            grp.extra_assertions = []  # type: ignore[attr-defined]
