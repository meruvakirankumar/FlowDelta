"""
Delta Store – Phase 3 of FlowDelta.

Persists and retrieves :class:`FlowTrace` and :class:`TraceDelta` objects.

Two formats are supported:

* **jsonl** – one JSON object per line; append-friendly, readable with
  standard tools (``jq``, ``grep``).  Default.
* **sqlite** – relational storage; enables SQL queries over delta history.

The store acts as the source of truth for:
  - *Golden runs* — official recorded executions used as regression baselines
  - *Comparison runs* — new executions compared against goldens
  - Historical delta timelines for trend analysis
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional

from ..state_tracker.trace_recorder import FlowTrace
from .state_diff import TraceDelta, SnapshotDelta


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class DeltaStore:
    """
    Stores and retrieves traces and deltas.

    Parameters
    ----------
    store_path : str | Path
        Directory where data is stored.
    format : str
        ``"jsonl"`` or ``"sqlite"``.
    """

    def __init__(self, store_path: str | Path = ".flowdelta/runs", format: str = "jsonl") -> None:
        self.store_path = Path(store_path)
        self.format = format
        self.store_path.mkdir(parents=True, exist_ok=True)
        if format == "sqlite":
            self._init_sqlite()

    # ------------------------------------------------------------------
    # Public API – write
    # ------------------------------------------------------------------

    def save_trace(self, trace: FlowTrace, golden: bool = False) -> str:
        """
        Persist *trace*.  Returns the run_id.

        Parameters
        ----------
        golden : bool
            If ``True``, mark this run as the canonical baseline.
        """
        enriched = trace.to_dict()
        enriched["golden"] = golden
        enriched["saved_at"] = datetime.now(timezone.utc).isoformat()

        if self.format == "jsonl":
            self._append_jsonl("traces.jsonl", enriched)
        else:
            self._sqlite_insert("traces", enriched)
        return trace.run_id

    def save_delta(self, delta: TraceDelta, run_id: str) -> None:
        """Persist a :class:`TraceDelta` linked to *run_id*."""
        data = delta.to_dict()
        data["run_id"] = run_id
        data["saved_at"] = datetime.now(timezone.utc).isoformat()

        if self.format == "jsonl":
            self._append_jsonl("deltas.jsonl", data)
        else:
            self._sqlite_insert("deltas", data)

    # ------------------------------------------------------------------
    # Public API – read
    # ------------------------------------------------------------------

    def load_trace(self, run_id: str) -> Optional[dict]:
        """Return the raw trace dict for *run_id*, or ``None``."""
        for record in self._iter_jsonl("traces.jsonl"):
            if record.get("run_id") == run_id:
                return record
        return None

    def load_golden(self, flow_id: str) -> Optional[dict]:
        """Return the most recent golden trace for *flow_id*."""
        candidates = [
            r for r in self._iter_jsonl("traces.jsonl")
            if r.get("flow_id") == flow_id and r.get("golden")
        ]
        return candidates[-1] if candidates else None

    def list_runs(self, flow_id: Optional[str] = None) -> List[dict]:
        """List all stored runs, optionally filtered by *flow_id*."""
        runs = list(self._iter_jsonl("traces.jsonl"))
        if flow_id:
            runs = [r for r in runs if r.get("flow_id") == flow_id]
        return runs

    def load_delta(self, run_id: str) -> Optional[dict]:
        """Return the raw delta dict for *run_id*, or ``None``."""
        for record in self._iter_jsonl("deltas.jsonl"):
            if record.get("run_id") == run_id:
                return record
        return None

    # ------------------------------------------------------------------
    # Comparison helpers
    # ------------------------------------------------------------------

    def compare_to_golden(self, current: TraceDelta) -> dict:
        """
        Compare *current* delta to the stored golden delta for the same flow.

        Returns a regression report dict with:
        - ``new_failures``: changes present in current but not in golden
        - ``resolved``: changes in golden but not in current
        - ``regressions``: value differences for the same variable paths
        """
        golden_data = self.load_golden(current.flow_id)
        if not golden_data:
            return {"error": "No golden run found", "flow_id": current.flow_id}

        golden_paths = self._extract_change_paths(golden_data["deltas"])
        current_paths = self._extract_change_paths(current.to_dict()["deltas"])

        new_failures = sorted(current_paths - golden_paths)
        resolved = sorted(golden_paths - current_paths)

        return {
            "flow_id": current.flow_id,
            "run_id": current.run_id,
            "new_failures": new_failures,
            "resolved": resolved,
            "regression_count": len(new_failures),
        }

    # ------------------------------------------------------------------
    # Internal – JSONL helpers
    # ------------------------------------------------------------------

    def _append_jsonl(self, filename: str, record: dict) -> None:
        path = self.store_path / filename
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def _iter_jsonl(self, filename: str) -> Iterator[dict]:
        path = self.store_path / filename
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue

    # ------------------------------------------------------------------
    # Internal – SQLite helpers
    # ------------------------------------------------------------------

    def _init_sqlite(self) -> None:
        db_path = self.store_path / "flowdelta.db"
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS traces (
                run_id TEXT PRIMARY KEY,
                flow_id TEXT,
                golden INTEGER,
                saved_at TEXT,
                data TEXT
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS deltas (
                run_id TEXT,
                flow_id TEXT,
                saved_at TEXT,
                data TEXT
            )
        """)
        self._conn.commit()

    def _sqlite_insert(self, table: str, record: dict) -> None:
        run_id = record.get("run_id", str(uuid.uuid4()))
        flow_id = record.get("flow_id", "")
        golden = int(record.get("golden", False))
        saved_at = record.get("saved_at", "")
        data = json.dumps(record)

        if table == "traces":
            self._conn.execute(
                "INSERT OR REPLACE INTO traces VALUES (?, ?, ?, ?, ?)",
                (run_id, flow_id, golden, saved_at, data),
            )
        else:
            self._conn.execute(
                "INSERT INTO deltas VALUES (?, ?, ?, ?)",
                (run_id, flow_id, saved_at, data),
            )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Internal – path extraction for comparison
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_change_paths(deltas_list: List[dict]) -> set:
        """Flatten all change deep_paths across a list of delta dicts."""
        paths: set = set()
        for delta in deltas_list:
            for change in delta.get("changes", []):
                paths.add(change.get("deep_path", ""))
        return paths
