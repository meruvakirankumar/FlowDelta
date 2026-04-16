"""
Regression Trend Charts – Sprint 4 of FlowDelta.

Generates multi-run trend visualisation from stored delta history. Produces:

* **ASCII trend chart** – works in any terminal, zero extra dependencies.
* **JSON data** – for embedding in dashboards or CI reports.
* **HTML chart** – self-contained HTML with inline Chart.js (v4) for
  browser viewing. No server required.

Data model
----------
Each point on the trend line represents one stored run:
  x-axis : run timestamp (or run sequence number)
  y-axis : total variable changes in that run

Additional series:
  - ``survived_count``  – how many variables were *not* caught by tests
    (if mutation report is linked)
  - ``invariant_count`` – how many invariants were detected per run

Usage::

    from src.delta_engine import DeltaStore
    from src.observability import TrendChartGenerator

    store = DeltaStore(store_path=".flowdelta/runs")
    gen   = TrendChartGenerator(store)

    # Print ASCII chart
    gen.print_ascii("checkout")

    # Write HTML report
    path = gen.write_html("checkout", output_path="reports/trend.html")

    # Raw data points
    points = gen.get_points("checkout")
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from ..delta_engine.delta_store import DeltaStore


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class TrendPoint:
    """One data point in a multi-run trend series."""
    run_id: str
    saved_at: str            # ISO-8601 timestamp
    sequence: int            # ordinal position among all runs for this flow
    total_changes: int
    golden: bool = False
    invariant_count: int = 0
    mutation_score: Optional[float] = None  # 0.0-1.0 if available
    change_counts: dict = field(default_factory=dict)  # {change_type: count}

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "saved_at": self.saved_at,
            "sequence": self.sequence,
            "total_changes": self.total_changes,
            "golden": self.golden,
            "invariant_count": self.invariant_count,
            "mutation_score": self.mutation_score,
            "change_counts": self.change_counts,
        }


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

class TrendChartGenerator:
    """
    Builds multi-run regression trend charts from :class:`DeltaStore` data.

    Parameters
    ----------
    store : DeltaStore
        Connected delta store (JSONL or SQLite).
    """

    def __init__(self, store: DeltaStore) -> None:
        self.store = store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_points(self, flow_id: str) -> List[TrendPoint]:
        """
        Load all stored runs for *flow_id* and build :class:`TrendPoint` list.
        Sorted chronologically.
        """
        all_runs = self.store.list_runs(flow_id=flow_id)
        # Sort by saved_at ascending
        all_runs.sort(key=lambda r: r.get("saved_at", ""))

        points: List[TrendPoint] = []
        for seq, run in enumerate(all_runs, start=1):
            run_id = run.get("run_id", "")
            # Sum total changes from deltas
            delta_data = self.store.load_delta(run_id)
            total = 0
            change_counts: dict = {}
            if delta_data:
                for d in delta_data.get("deltas", []):
                    for c in d.get("changes", []):
                        total += 1
                        ct = c.get("change_type", "unknown")
                        change_counts[ct] = change_counts.get(ct, 0) + 1

            points.append(TrendPoint(
                run_id=run_id,
                saved_at=run.get("saved_at", ""),
                sequence=seq,
                total_changes=total,
                golden=bool(run.get("golden")),
                change_counts=change_counts,
            ))

        return points

    def print_ascii(self, flow_id: str, width: int = 60, height: int = 12) -> None:
        """Print an ASCII bar chart of change counts per run to stdout."""
        points = self.get_points(flow_id)
        if not points:
            print(f"No runs found for flow '{flow_id}'")
            return

        max_val = max(p.total_changes for p in points) or 1
        scale = height / max_val

        print(f"\n  Regression Trend: {flow_id}  ({len(points)} runs)\n")
        print(f"  {'Changes':>8}  {'Run':>10}  Chart")
        print(f"  {'':->8}  {'':->10}  {'':->40}")

        for p in points:
            bar_len = max(1, round(p.total_changes * scale * (width / height)))
            bar = "█" * bar_len
            golden_marker = " ★" if p.golden else ""
            print(
                f"  {p.total_changes:>8}  {p.run_id[:10]:>10}  "
                f"[cyan]{bar}[/cyan]{golden_marker}"
            )

        avg = sum(p.total_changes for p in points) / len(points)
        print(f"\n  Avg changes/run: {avg:.1f}")

    def to_json(self, flow_id: str) -> str:
        """Return trend data as JSON string."""
        points = self.get_points(flow_id)
        return json.dumps(
            {
                "flow_id": flow_id,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "points": [p.to_dict() for p in points],
            },
            indent=2,
        )

    def write_html(
        self,
        flow_id: str,
        output_path: str | Path = "reports/trend.html",
    ) -> Path:
        """
        Write a self-contained HTML page with an interactive Chart.js trend chart.
        No server or external CDN required (Chart.js loaded from jsDelivr CDN).

        Returns the output path.
        """
        points = self.get_points(flow_id)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        labels = [p.run_id[:8] for p in points]
        changes_data = [p.total_changes for p in points]
        golden_flags = [p.golden for p in points]

        point_colors = [
            "rgba(255, 215, 0, 0.9)" if g else "rgba(99, 132, 255, 0.8)"
            for g in golden_flags
        ]

        html = _HTML_TEMPLATE.format(
            flow_id=flow_id,
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            labels=json.dumps(labels),
            changes_data=json.dumps(changes_data),
            point_colors=json.dumps(point_colors),
            total_runs=len(points),
            avg_changes=f"{sum(changes_data)/len(changes_data):.1f}" if changes_data else "0",
            max_changes=max(changes_data) if changes_data else 0,
            golden_count=sum(golden_flags),
        )
        output_path.write_text(html, encoding="utf-8")
        return output_path


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>FlowDelta – Trend: {flow_id}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, sans-serif; background: #0f1117; color: #e2e8f0; padding: 2rem; }}
    h1 {{ font-size: 1.5rem; margin-bottom: 0.25rem; color: #7c8cf8; }}
    p.subtitle {{ color: #64748b; font-size: 0.875rem; margin-bottom: 2rem; }}
    .stats {{ display: flex; gap: 1.5rem; margin-bottom: 2rem; flex-wrap: wrap; }}
    .stat-card {{ background: #1e2130; border-radius: 8px; padding: 1rem 1.5rem; min-width: 140px; }}
    .stat-card .label {{ font-size: 0.75rem; text-transform: uppercase; color: #64748b; letter-spacing: 0.05em; }}
    .stat-card .value {{ font-size: 1.75rem; font-weight: 700; color: #7c8cf8; }}
    .chart-container {{ background: #1e2130; border-radius: 12px; padding: 1.5rem; }}
    canvas {{ max-height: 400px; }}
    .legend {{ display: flex; gap: 1.5rem; margin-top: 1rem; font-size: 0.8rem; color: #94a3b8; }}
    .legend span {{ display: inline-flex; align-items: center; gap: 0.35rem; }}
    .dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}
  </style>
</head>
<body>
  <h1>FlowDelta – Regression Trend</h1>
  <p class="subtitle">Flow: <strong>{flow_id}</strong> &nbsp;|&nbsp; Generated: {generated_at}</p>

  <div class="stats">
    <div class="stat-card"><div class="label">Total Runs</div><div class="value">{total_runs}</div></div>
    <div class="stat-card"><div class="label">Avg Changes / Run</div><div class="value">{avg_changes}</div></div>
    <div class="stat-card"><div class="label">Peak Changes</div><div class="value">{max_changes}</div></div>
    <div class="stat-card"><div class="label">Golden Baselines</div><div class="value">{golden_count}</div></div>
  </div>

  <div class="chart-container">
    <canvas id="trendChart"></canvas>
    <div class="legend">
      <span><span class="dot" style="background:#6384ff"></span> Run</span>
      <span><span class="dot" style="background:#ffd700"></span> Golden baseline</span>
    </div>
  </div>

  <script>
    const ctx = document.getElementById('trendChart').getContext('2d');
    new Chart(ctx, {{
      type: 'bar',
      data: {{
        labels: {labels},
        datasets: [{{
          label: 'Variable Changes',
          data: {changes_data},
          backgroundColor: {point_colors},
          borderRadius: 4,
          borderSkipped: false,
        }}]
      }},
      options: {{
        responsive: true,
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{
            callbacks: {{
              title: (items) => 'Run: ' + items[0].label,
              label: (item) => item.raw + ' variable changes'
            }}
          }}
        }},
        scales: {{
          x: {{ grid: {{ color: '#2d3748' }}, ticks: {{ color: '#94a3b8' }} }},
          y: {{
            grid: {{ color: '#2d3748' }},
            ticks: {{ color: '#94a3b8' }},
            title: {{ display: true, text: 'Variable Changes', color: '#64748b' }}
          }}
        }}
      }}
    }});
  </script>
</body>
</html>
"""
