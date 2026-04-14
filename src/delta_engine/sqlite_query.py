"""
SQLite Query API – Sprint 2 of FlowDelta.

Provides a rich SQL-backed query interface over the FlowDelta delta store
when ``format="sqlite"`` is configured.

Capabilities
------------
* ``flows_summary()``       — per-flow run count, total changes, last run time
* ``run_history()``         — ordered run list for a specific flow
* ``hot_variables()``       — which variables change most frequently
* ``regression_trend()``    — change count per run over time (trend line)
* ``search_changes()``      — find runs where a specific variable changed
* ``compare_runs()``        — side-by-side diff of two runs' change paths
* ``delete_run()``          — remove a single run (traces + deltas)
* ``vacuum()``              — reclaim disk space

All query methods return plain Python dicts/lists — no ORM objects — so
they can be serialized directly to JSON or displayed in a table.

Schema (created by DeltaStore._init_sqlite)
-------------------------------------------
traces  (run_id PK, flow_id, golden, saved_at, data JSON)
deltas  (run_id, flow_id, saved_at, data JSON)

The ``data`` column stores the full serialized dict, so we use
``json_extract`` for lightweight indexed queries and in-process JSON
deserialization for complex aggregations.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


class SQLiteQueryAPI:
    """
    Rich query interface over a FlowDelta SQLite database.

    Parameters
    ----------
    store_path : str | Path
        Same ``store_path`` used when creating :class:`DeltaStore` with
        ``format="sqlite"``.  The ``flowdelta.db`` file is expected inside
        this directory.
    """

    def __init__(self, store_path: str | Path = ".flowdelta/runs") -> None:
        db_path = Path(store_path) / "flowdelta.db"
        if not db_path.exists():
            raise FileNotFoundError(
                f"No FlowDelta SQLite database found at {db_path}. "
                "Run FlowDelta with format='sqlite' first."
            )
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._ensure_indexes()

    # ------------------------------------------------------------------
    # Schema helpers
    # ------------------------------------------------------------------

    def _ensure_indexes(self) -> None:
        """Create indexes on first use for fast lookups."""
        with self._conn:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_traces_flow "
                "ON traces(flow_id, saved_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_traces_golden "
                "ON traces(flow_id, golden)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_deltas_run "
                "ON deltas(run_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_deltas_flow "
                "ON deltas(flow_id, saved_at)"
            )

    # ------------------------------------------------------------------
    # 1. flows_summary()
    # ------------------------------------------------------------------

    def flows_summary(self) -> List[Dict[str, Any]]:
        """
        Return one summary row per flow::

            [
              {
                "flow_id":      "checkout",
                "total_runs":   12,
                "golden_runs":  1,
                "last_run_at":  "2026-04-14T10:00:00+00:00",
                "total_changes": 87,
              },
              ...
            ]
        """
        rows = self._conn.execute(
            """
            SELECT
                flow_id,
                COUNT(*)              AS total_runs,
                SUM(golden)           AS golden_runs,
                MAX(saved_at)         AS last_run_at
            FROM traces
            GROUP BY flow_id
            ORDER BY last_run_at DESC
            """
        ).fetchall()

        results = []
        for row in rows:
            flow_id = row["flow_id"]
            total_changes = self._total_changes_for_flow(flow_id)
            results.append({
                "flow_id":       flow_id,
                "total_runs":    row["total_runs"],
                "golden_runs":   row["golden_runs"] or 0,
                "last_run_at":   row["last_run_at"],
                "total_changes": total_changes,
            })
        return results

    # ------------------------------------------------------------------
    # 2. run_history()
    # ------------------------------------------------------------------

    def run_history(
        self,
        flow_id: str,
        limit: int = 50,
        golden_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Return ordered run history for *flow_id*, newest first.

        Each entry::

            {
              "run_id":    "abc12345",
              "golden":    False,
              "saved_at":  "2026-04-14T10:00:00+00:00",
              "snapshot_count": 8,
              "change_count":   5,
            }
        """
        query = """
            SELECT run_id, golden, saved_at, data
            FROM traces
            WHERE flow_id = ?
            {}
            ORDER BY saved_at DESC
            LIMIT ?
        """.format("AND golden = 1" if golden_only else "")

        rows = self._conn.execute(query, (flow_id, limit)).fetchall()
        results = []
        for row in rows:
            data = json.loads(row["data"])
            snapshot_count = len(data.get("snapshots", []))
            change_count = self._change_count_for_run(row["run_id"])
            results.append({
                "run_id":         row["run_id"],
                "golden":         bool(row["golden"]),
                "saved_at":       row["saved_at"],
                "snapshot_count": snapshot_count,
                "change_count":   change_count,
            })
        return results

    # ------------------------------------------------------------------
    # 3. hot_variables()
    # ------------------------------------------------------------------

    def hot_variables(
        self,
        flow_id: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Return the variables that changed most frequently across all runs
        of *flow_id*.

        Each entry::

            {
              "variable":    "cart",
              "change_count": 42,
              "change_types": {"changed": 35, "added": 7},
            }
        """
        delta_rows = self._conn.execute(
            "SELECT data FROM deltas WHERE flow_id = ?",
            (flow_id,),
        ).fetchall()

        freq: Dict[str, Dict[str, int]] = {}
        for row in delta_rows:
            data = json.loads(row["data"])
            for transition in data.get("deltas", []):
                for change in transition.get("changes", []):
                    var = change.get("name", "")
                    ctype = change.get("change_type", "")
                    freq.setdefault(var, {})
                    freq[var][ctype] = freq[var].get(ctype, 0) + 1

        sorted_vars = sorted(
            freq.items(),
            key=lambda kv: sum(kv[1].values()),
            reverse=True,
        )

        return [
            {
                "variable":    var,
                "change_count": sum(ctypes.values()),
                "change_types": ctypes,
            }
            for var, ctypes in sorted_vars[:limit]
        ]

    # ------------------------------------------------------------------
    # 4. regression_trend()
    # ------------------------------------------------------------------

    def regression_trend(self, flow_id: str) -> List[Dict[str, Any]]:
        """
        Return change count per run, ordered chronologically — useful for
        plotting a trend chart.

        Each entry::

            {
              "run_id":       "abc12345",
              "saved_at":     "2026-04-14T10:00:00+00:00",
              "change_count":  5,
            }
        """
        delta_rows = self._conn.execute(
            """
            SELECT d.run_id, t.saved_at, d.data
            FROM deltas d
            JOIN traces t ON d.run_id = t.run_id
            WHERE d.flow_id = ?
            ORDER BY t.saved_at ASC
            """,
            (flow_id,),
        ).fetchall()

        results = []
        for row in delta_rows:
            data = json.loads(row["data"])
            count = sum(
                len(tr.get("changes", []))
                for tr in data.get("deltas", [])
            )
            results.append({
                "run_id":       row["run_id"],
                "saved_at":     row["saved_at"],
                "change_count": count,
            })
        return results

    # ------------------------------------------------------------------
    # 5. search_changes()
    # ------------------------------------------------------------------

    def search_changes(
        self,
        variable_name: str,
        flow_id: Optional[str] = None,
        change_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Find all runs where *variable_name* changed.

        Parameters
        ----------
        variable_name : str
            Exact top-level variable name to search for.
        flow_id : str | None
            Restrict to a specific flow.
        change_type : str | None
            Filter by change type: ``"changed"``, ``"added"``, ``"removed"``,
            ``"type_changed"``.

        Each entry::

            {
              "run_id":      "abc12345",
              "flow_id":     "checkout",
              "saved_at":    "2026-04-14T10:00:00+00:00",
              "location":    "ecommerce.py:45 (process_payment)",
              "change_type": "changed",
              "old_value":   0,
              "new_value":   99.99,
            }
        """
        query = "SELECT d.run_id, d.flow_id, t.saved_at, d.data FROM deltas d JOIN traces t ON d.run_id = t.run_id"
        params: list = []
        conditions = []
        if flow_id:
            conditions.append("d.flow_id = ?")
            params.append(flow_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY t.saved_at DESC"

        rows = self._conn.execute(query, params).fetchall()
        results = []
        for row in rows:
            data = json.loads(row["data"])
            for transition in data.get("deltas", []):
                for change in transition.get("changes", []):
                    if change.get("name") != variable_name:
                        continue
                    if change_type and change.get("change_type") != change_type:
                        continue
                    results.append({
                        "run_id":       row["run_id"],
                        "flow_id":      row["flow_id"],
                        "saved_at":     row["saved_at"],
                        "location":     transition.get("to_location", ""),
                        "change_type":  change.get("change_type"),
                        "old_value":    change.get("old_value"),
                        "new_value":    change.get("new_value"),
                    })
        return results

    # ------------------------------------------------------------------
    # 6. compare_runs()
    # ------------------------------------------------------------------

    def compare_runs(self, run_id_a: str, run_id_b: str) -> Dict[str, Any]:
        """
        Side-by-side comparison of the change paths in two runs.

        Returns::

            {
              "run_a": "abc12345",
              "run_b": "def67890",
              "only_in_a":  ["root['cart']['total']", ...],
              "only_in_b":  ["root['user']['verified']", ...],
              "in_both":    ["root['status']", ...],
            }
        """
        def load_paths(run_id: str) -> set:
            row = self._conn.execute(
                "SELECT data FROM deltas WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not row:
                return set()
            data = json.loads(row["data"])
            return {
                change.get("deep_path", "")
                for tr in data.get("deltas", [])
                for change in tr.get("changes", [])
            }

        paths_a = load_paths(run_id_a)
        paths_b = load_paths(run_id_b)

        return {
            "run_a":      run_id_a,
            "run_b":      run_id_b,
            "only_in_a":  sorted(paths_a - paths_b),
            "only_in_b":  sorted(paths_b - paths_a),
            "in_both":    sorted(paths_a & paths_b),
        }

    # ------------------------------------------------------------------
    # 7. delete_run()
    # ------------------------------------------------------------------

    def delete_run(self, run_id: str) -> int:
        """
        Delete all traces and deltas for *run_id*.

        Returns the total number of rows deleted.
        """
        with self._conn:
            t = self._conn.execute(
                "DELETE FROM traces WHERE run_id = ?", (run_id,)
            ).rowcount
            d = self._conn.execute(
                "DELETE FROM deltas WHERE run_id = ?", (run_id,)
            ).rowcount
        return t + d

    # ------------------------------------------------------------------
    # 8. vacuum()
    # ------------------------------------------------------------------

    def vacuum(self) -> None:
        """Rebuild the database file to reclaim space after deletions."""
        self._conn.execute("VACUUM")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _total_changes_for_flow(self, flow_id: str) -> int:
        rows = self._conn.execute(
            "SELECT data FROM deltas WHERE flow_id = ?", (flow_id,)
        ).fetchall()
        return sum(
            len(change_list)
            for row in rows
            for tr in json.loads(row["data"]).get("deltas", [])
            for change_list in [tr.get("changes", [])]
        )

    def _change_count_for_run(self, run_id: str) -> int:
        row = self._conn.execute(
            "SELECT data FROM deltas WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not row:
            return 0
        data = json.loads(row["data"])
        return sum(len(tr.get("changes", [])) for tr in data.get("deltas", []))

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SQLiteQueryAPI":
        return self

    def __exit__(self, *_) -> None:
        self.close()
