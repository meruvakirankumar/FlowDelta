"""
run_flows.py – Demonstrates FlowDelta end-to-end on the sample e-commerce app.

Run from the project root:
    python examples/sample_app/run_flows.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow importing src/ from project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.flow_identifier import ASTAnalyzer, CallGraphBuilder, LLMFlowMapper
from src.state_tracker import SysTraceRecorder, FlowTrace
from src.delta_engine import StateDiffer, DeltaStore
from src.test_generator import AssertionGenerator, LLMTestWriter, TestRenderer

import uuid

# ---------------------------------------------------------------------------
# Point to the sample app
# ---------------------------------------------------------------------------
APP_PATH = Path(__file__).parent / "ecommerce.py"
PROJECT_ROOT = Path(__file__).parent.parent.parent


def main() -> None:
    # ------------------------------------------------------------------ #
    # Phase 1 – Flow Identification                                        #
    # ------------------------------------------------------------------ #
    print("\n" + "=" * 60)
    print(" Phase 1: Identifying flows via AST + LLM")
    print("=" * 60)

    analyzer = ASTAnalyzer()
    analysis = analyzer.analyze_file(APP_PATH)
    print(f"  Functions found: {[f.qualified_name for f in analysis.functions]}")

    cg = CallGraphBuilder().build([analysis])
    print(f"  Entry points: {cg.entry_points}")

    # Uses heuristic flow mapping (no API key needed for demo)
    flow_map = LLMFlowMapper(max_flows=5).identify_flows(cg)
    print(f"  Flows identified: {[f.id for f in flow_map.flows]}")

    # ------------------------------------------------------------------ #
    # Phase 2 – Record a trace for the 'checkout' flow                    #
    # ------------------------------------------------------------------ #
    print("\n" + "=" * 60)
    print(" Phase 2: Recording state trace")
    print("=" * 60)

    from examples.sample_app.ecommerce import checkout  # noqa

    watch_fns = {"checkout", "build_cart", "apply_coupon", "process_payment", "create_order"}
    recorder = SysTraceRecorder(watch_functions=watch_fns, line_level=False, max_depth=3)

    recorder.record(
        checkout,
        user_id="user-42",
        product_quantities={"p001": 1, "p002": 2},
        coupon="SAVE10",
    )

    run_id = str(uuid.uuid4())[:8]
    trace = FlowTrace(
        flow_id="checkout",
        run_id=run_id,
        snapshots=recorder.snapshots,
    )
    print(f"  Captured {len(trace.snapshots)} snapshots (run_id={run_id})")

    # ------------------------------------------------------------------ #
    # Phase 3 – Compute deltas                                            #
    # ------------------------------------------------------------------ #
    print("\n" + "=" * 60)
    print(" Phase 3: Computing state deltas")
    print("=" * 60)

    differ = StateDiffer()
    td = differ.diff_trace(trace)
    td.print_report()

    # Persist
    store = DeltaStore(store_path=PROJECT_ROOT / ".flowdelta" / "runs")
    store.save_trace(trace, golden=True)
    store.save_delta(td, run_id)
    print(f"\n  Stored golden trace → .flowdelta/runs/traces.jsonl")

    # ------------------------------------------------------------------ #
    # Phase 4 – Generate tests                                            #
    # ------------------------------------------------------------------ #
    print("\n" + "=" * 60)
    print(" Phase 4: Generating pytest tests")
    print("=" * 60)

    spec = AssertionGenerator().generate(td)
    # LLMTestWriter.augment() uses heuristic names when no API key set
    spec = LLMTestWriter().augment(spec)
    out = TestRenderer(
        template_dir=PROJECT_ROOT / "templates",
        output_dir=PROJECT_ROOT / "generated_tests",
    ).render(spec)
    print(f"  Test file written → {out}")
    print("\n  Run with:  pytest", out.name)


if __name__ == "__main__":
    # Make sure the package root is on sys.path
    sys.path.insert(0, str(PROJECT_ROOT := Path(__file__).parent.parent.parent))
    main()
