"""
FlowDelta Orchestrator – main pipeline + CLI.

Ties all phases together into a single command-line tool:

  flowdelta analyze   <src_dir>          — Phase 1: identify flows
  flowdelta record    <flow_id> <script>  — Phase 2: record a trace
  flowdelta diff      <run_id>            — Phase 3: compute deltas
  flowdelta generate  <flow_id>           — Phase 4: generate tests
  flowdelta run       <src_dir> <script>  — Full pipeline in one shot
  flowdelta compare   <flow_id> <run_id>  — Compare run to golden
  flowdelta report    <flow_id>           — Print delta report

Configuration is loaded from ``config/config.yaml`` by default.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Callable, Optional

import click
import yaml
from rich.console import Console
from rich.table import Table

from .flow_identifier import ASTAnalyzer, CallGraphBuilder, LLMFlowMapper
from .state_tracker import SysTraceRecorder, FlowTrace
from .state_tracker.dap_client import StateSnapshot
from .delta_engine import StateDiffer, DeltaStore
from .test_generator import AssertionGenerator, LLMTestWriter, TestRenderer

console = Console()
logger = logging.getLogger("flowdelta")


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(config_path: str = "config/config.yaml") -> dict:
    path = Path(config_path)
    if not path.exists():
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Pipeline class
# ---------------------------------------------------------------------------

class FlowDeltaPipeline:
    """
    Programmatic interface to the full FlowDelta pipeline.

    Example::

        pipeline = FlowDeltaPipeline(config_path="config/config.yaml")
        flows = pipeline.analyze("src/")
        trace = pipeline.record(flows[0], lambda: my_app())
        delta = pipeline.diff(trace)
        test_path = pipeline.generate(delta)
        print(f"Tests written to {test_path}")
    """

    def __init__(self, config_path: str = "config/config.yaml") -> None:
        self.cfg = load_config(config_path)
        llm_cfg = self.cfg.get("llm", {})
        tracker_cfg = self.cfg.get("state_tracker", {})
        delta_cfg = self.cfg.get("delta_engine", {})
        gen_cfg = self.cfg.get("test_generator", {})

        api_key = os.environ.get(llm_cfg.get("api_key_env", "OPENAI_API_KEY"), "")
        model = llm_cfg.get("model", "gpt-4o")

        self.analyzer = ASTAnalyzer()
        self.graph_builder = CallGraphBuilder()
        self.flow_mapper = LLMFlowMapper(
            model=model,
            api_key=api_key,
            max_flows=self.cfg.get("flow_identifier", {}).get("max_flows", 20),
        )
        self.differ = StateDiffer(
            ignore_order=delta_cfg.get("diff_options", {}).get("ignore_order", False),
            significant_digits=delta_cfg.get("diff_options", {}).get("significant_digits", 5),
        )
        self.store = DeltaStore(
            store_path=delta_cfg.get("store_path", ".flowdelta/runs"),
            format=delta_cfg.get("format", "jsonl"),
        )
        self.assertion_gen = AssertionGenerator()
        self.llm_writer = LLMTestWriter(model=model, api_key=api_key)

        project_root = str(Path(config_path).parent.parent)
        self.renderer = TestRenderer(
            template_dir=Path(project_root) / "templates",
            output_dir=gen_cfg.get("output_dir", "generated_tests"),
        )

        capture_cfg = tracker_cfg.get("capture", {})
        self._line_level = False
        self._max_depth = capture_cfg.get("max_depth", 4)
        self._skip_private = capture_cfg.get("skip_private", True)

    # ------------------------------------------------------------------
    # Phase 1: Analyze
    # ------------------------------------------------------------------

    def analyze(self, src_dir: str):
        """
        Analyze all source files in *src_dir*, build a call graph, and
        identify flows using the LLM.

        Returns a :class:`FlowMap`.
        """
        console.print(f"[bold cyan]Phase 1:[/bold cyan] Analyzing [green]{src_dir}[/green]")
        analyses = self.analyzer.analyze_directory(src_dir)
        console.print(f"  Parsed {len(analyses)} files")

        cg = self.graph_builder.build(analyses)
        console.print(
            f"  Call graph: {cg.graph.number_of_nodes()} nodes, "
            f"{cg.graph.number_of_edges()} edges, "
            f"{len(cg.entry_points)} entry points"
        )

        console.print("  Identifying flows via LLM…")
        flow_map = self.flow_mapper.identify_flows(cg)
        console.print(f"  Found [bold]{len(flow_map.flows)}[/bold] flows")

        for flow in flow_map.flows:
            console.print(f"    • [yellow]{flow.id}[/yellow]: {flow.description}")

        return flow_map

    # ------------------------------------------------------------------
    # Phase 2: Record
    # ------------------------------------------------------------------

    def record(
        self,
        flow,    # Flow object
        callable_: Callable,
        *args,
        golden: bool = False,
        **kwargs,
    ) -> FlowTrace:
        """
        Execute *callable_* under tracing and return the :class:`FlowTrace`.
        """
        console.print(
            f"[bold cyan]Phase 2:[/bold cyan] Recording flow [yellow]{flow.id}[/yellow]"
        )
        watch_fns = {s.function for s in flow.steps}
        recorder = SysTraceRecorder(
            watch_functions=watch_fns,
            line_level=self._line_level,
            max_depth=self._max_depth,
            skip_private=self._skip_private,
        )
        run_id = str(uuid.uuid4())[:8]
        recorder.record(callable_, *args, **kwargs)
        trace = FlowTrace(
            flow_id=flow.id,
            run_id=run_id,
            snapshots=recorder.snapshots,
        )
        console.print(
            f"  Captured [bold]{len(trace.snapshots)}[/bold] snapshots "
            f"(run_id={run_id})"
        )
        self.store.save_trace(trace, golden=golden)
        return trace

    # ------------------------------------------------------------------
    # Phase 3: Diff
    # ------------------------------------------------------------------

    def diff(self, trace: FlowTrace) -> "TraceDelta":
        """Compute and store deltas for *trace*."""
        from .delta_engine import TraceDelta
        console.print(
            f"[bold cyan]Phase 3:[/bold cyan] Computing deltas for run "
            f"[yellow]{trace.run_id}[/yellow]"
        )
        td = self.differ.diff_trace(trace)
        self.store.save_delta(td, trace.run_id)
        console.print(
            f"  {len(td.deltas)} transitions, "
            f"[bold]{td.total_changes}[/bold] variable changes"
        )
        return td

    # ------------------------------------------------------------------
    # Phase 4: Generate
    # ------------------------------------------------------------------

    def generate(self, delta) -> Path:
        """Generate a pytest file from *delta* and return its path."""
        console.print(
            f"[bold cyan]Phase 4:[/bold cyan] Generating tests for "
            f"flow [yellow]{delta.flow_id}[/yellow]"
        )
        spec = self.assertion_gen.generate(delta)
        spec = self.llm_writer.augment(spec)
        out = self.renderer.render(spec)
        console.print(f"  Tests written → [green]{out}[/green]")
        return out

    # ------------------------------------------------------------------
    # Full pipeline
    # ------------------------------------------------------------------

    def run_full(
        self,
        src_dir: str,
        callable_: Callable,
        *args,
        golden: bool = False,
        **kwargs,
    ) -> Path:
        """Execute all 4 phases end-to-end."""
        flow_map = self.analyze(src_dir)
        if not flow_map.flows:
            console.print("[red]No flows identified. Aborting.[/red]")
            raise SystemExit(1)

        # Record each identified flow
        out_paths = []
        for flow in flow_map.flows:
            trace = self.record(flow, callable_, *args, golden=golden, **kwargs)
            delta = self.diff(trace)
            out = self.generate(delta)
            out_paths.append(out)

        return out_paths[0] if out_paths else Path("generated_tests")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.option("--config", default="config/config.yaml", help="Path to config.yaml")
@click.option("--verbose", is_flag=True)
@click.pass_context
def cli(ctx: click.Context, config: str, verbose: bool) -> None:
    """FlowDelta – AI-powered flow tracing and delta-based test generation."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING)
    ctx.ensure_object(dict)
    ctx.obj["config"] = config
    ctx.obj["pipeline"] = FlowDeltaPipeline(config_path=config)


@cli.command()
@click.argument("src_dir")
@click.option("--output", default="flows.json", help="Save flow map to file")
@click.pass_context
def analyze(ctx: click.Context, src_dir: str, output: str) -> None:
    """Phase 1: Identify application flows in SRC_DIR."""
    pipeline: FlowDeltaPipeline = ctx.obj["pipeline"]
    flow_map = pipeline.analyze(src_dir)
    Path(output).write_text(
        json.dumps(flow_map.to_dict(), indent=2), encoding="utf-8"
    )
    console.print(f"Flow map saved → [green]{output}[/green]")


@cli.command()
@click.argument("flow_id")
@click.argument("script")
@click.option("--golden", is_flag=True, help="Mark this run as the golden baseline")
@click.option("--flows-json", default="flows.json")
@click.pass_context
def record(
    ctx: click.Context,
    flow_id: str,
    script: str,
    golden: bool,
    flows_json: str,
) -> None:
    """Phase 2: Run SCRIPT and record state trace for FLOW_ID."""
    pipeline: FlowDeltaPipeline = ctx.obj["pipeline"]

    # Load flows
    flows_data = json.loads(Path(flows_json).read_text())
    flow = next(
        (f for f in flows_data["flows"] if f["id"] == flow_id), None
    )
    if not flow:
        console.print(f"[red]Flow '{flow_id}' not found in {flows_json}[/red]")
        raise SystemExit(1)

    # Wrap the script as a callable
    def run_script() -> None:
        import runpy
        runpy.run_path(script, run_name="__main__")

    # Minimal Flow shim
    class _F:
        id = flow_id
        steps = [type("S", (), {"function": s["function"]})() for s in flow.get("steps", [])]

    trace = pipeline.record(_F(), run_script, golden=golden)
    console.print(f"Run ID: [bold]{trace.run_id}[/bold]")


@cli.command()
@click.argument("run_id")
@click.pass_context
def diff(ctx: click.Context, run_id: str) -> None:
    """Phase 3: Compute deltas for RUN_ID."""
    pipeline: FlowDeltaPipeline = ctx.obj["pipeline"]
    raw = pipeline.store.load_trace(run_id)
    if not raw:
        console.print(f"[red]Run '{run_id}' not found.[/red]")
        raise SystemExit(1)

    # Reconstruct trace from stored dict
    snapshots = []
    for s in raw.get("snapshots", []):
        snapshots.append(StateSnapshot(
            event=s["event"],
            thread_id=s["thread_id"],
            file=s["file"],
            line=s["line"],
            function=s["function"],
            locals=s["locals"],
            sequence=s["sequence"],
        ))
    trace = FlowTrace(
        flow_id=raw["flow_id"],
        run_id=run_id,
        snapshots=snapshots,
    )
    delta = pipeline.diff(trace)
    delta.print_report()


@cli.command()
@click.argument("flow_id")
@click.option("--run-id", default=None, help="Specific run to generate from (default: latest)")
@click.pass_context
def generate(ctx: click.Context, flow_id: str, run_id: Optional[str]) -> None:
    """Phase 4: Generate pytest tests for FLOW_ID."""
    pipeline: FlowDeltaPipeline = ctx.obj["pipeline"]
    delta_data = (
        pipeline.store.load_delta(run_id) if run_id
        else pipeline.store.load_golden(flow_id)
    )
    if not delta_data:
        console.print(f"[red]No stored delta found for flow '{flow_id}'.[/red]")
        raise SystemExit(1)

    # Reconstruct TraceDelta from stored dict
    from .delta_engine.state_diff import TraceDelta, SnapshotDelta, VariableDelta
    sd_list = []
    for d in delta_data.get("deltas", []):
        changes = [
            VariableDelta(
                name=c["name"],
                change_type=c["change_type"],
                old_value=c.get("old_value"),
                new_value=c.get("new_value"),
                old_type=c.get("old_type"),
                new_type=c.get("new_type"),
                deep_path=c.get("deep_path", ""),
            )
            for c in d.get("changes", [])
        ]
        sd_list.append(SnapshotDelta(
            from_seq=d["from_seq"],
            to_seq=d["to_seq"],
            from_location=d["from_location"],
            to_location=d["to_location"],
            changes=changes,
        ))
    td = TraceDelta(
        flow_id=flow_id,
        run_id=delta_data.get("run_id", ""),
        deltas=sd_list,
    )
    pipeline.generate(td)


@cli.command()
@click.argument("flow_id")
@click.argument("run_id")
@click.pass_context
def compare(ctx: click.Context, flow_id: str, run_id: str) -> None:
    """Compare RUN_ID against the golden run for FLOW_ID."""
    pipeline: FlowDeltaPipeline = ctx.obj["pipeline"]
    delta_data = pipeline.store.load_delta(run_id)
    if not delta_data:
        console.print(f"[red]No delta for run '{run_id}'.[/red]")
        raise SystemExit(1)

    from .delta_engine.state_diff import TraceDelta
    td = TraceDelta(flow_id=flow_id, run_id=run_id)
    report = pipeline.store.compare_to_golden(td)

    table = Table(title=f"Regression Report: {flow_id}")
    table.add_column("Category", style="cyan")
    table.add_column("Count", style="bold")
    table.add_row("New failures", str(report.get("regression_count", 0)))
    table.add_row("Resolved regressions", str(len(report.get("resolved", []))))
    console.print(table)

    if report.get("new_failures"):
        console.print("[red]New failures:[/red]")
        for f in report["new_failures"]:
            console.print(f"  {f}")


if __name__ == "__main__":
    cli()
