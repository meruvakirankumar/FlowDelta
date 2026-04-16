"""
Shared test helpers for the FlowDelta test suite.

Import with::

    from helpers import make_snapshot, FakeDeltaStore
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def make_snapshot(
    seq: int,
    locals_: Optional[Dict[str, Any]] = None,
    *,
    file: str = "app.py",
    line: int = 1,
    fn: str = "fn",
    event: str = "call",
    thread_id: int = 0,
):
    """Create a minimal :class:`StateSnapshot` for unit tests."""
    from src.state_tracker.dap_client import StateSnapshot

    return StateSnapshot(
        event=event,
        thread_id=thread_id,
        file=file,
        line=line,
        function=fn,
        locals=locals_ or {},
        sequence=seq,
    )


class FakeDeltaStore:
    """
    Minimal read-only fake DeltaStore for unit tests.

    Accepts the same ``list_runs`` / ``load_delta`` / ``load_trace`` interface
    used by :class:`TrendChartGenerator` and :class:`DeltaDashboard`.
    """

    store_path = ".flowdelta/test"

    def __init__(
        self,
        runs: List[Dict[str, Any]],
        deltas: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._runs = runs
        self._deltas = deltas or {}

    def list_runs(self, flow_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if flow_id:
            return [r for r in self._runs if r.get("flow_id") == flow_id]
        return list(self._runs)

    def load_delta(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self._deltas.get(run_id)

    def load_trace(self, run_id: str) -> None:
        return None
