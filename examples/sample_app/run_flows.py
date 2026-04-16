"""
run_flows.py – Demonstrates the FlowDelta SDK on the sample e-commerce app.

This file shows how to integrate FlowDelta into *any* Python application
pipeline using the high-level SDK API.  Swap out ``checkout`` and
``watch_fns`` for your own entry-point function and helper names.

Run from the project root::

    python examples/sample_app/run_flows.py

To integrate FlowDelta into YOUR own project:
1. ``flowdelta init /path/to/your-project`` — generate a config file
2. Copy the pattern below, replacing ``checkout`` with your pipeline function
3. Run your script — traces, deltas, and tests are generated automatically
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow importing src/ from project root when running as a script
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from flowdelta import FlowDelta  # noqa: E402 — needs sys.path patched first
from examples.sample_app.ecommerce import checkout  # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent.parent

# ---------------------------------------------------------------------------
# Step 1 – Create a FlowDelta instance pointing at this project's config.
#
# Replace the paths below with your own project's layout, or omit them
# entirely to use FlowDelta defaults (.flowdelta/runs, generated_tests/).
# ---------------------------------------------------------------------------
fd = FlowDelta(
    src_dir=PROJECT_ROOT / "examples" / "sample_app",
    config_path=str(PROJECT_ROOT / "config" / "config.yaml"),
    store_path=str(PROJECT_ROOT / ".flowdelta" / "runs"),
    output_dir=str(PROJECT_ROOT / "generated_tests"),
)

# ---------------------------------------------------------------------------
# Step 2 – Observe your pipeline entry point.
#
# Replace ``checkout`` with YOUR function.
# ``watch_functions`` lists every sub-function whose state you want captured.
# Omit ``watch_functions`` to auto-detect all public functions in the module.
# Set ``golden=True`` on the first run to create a regression baseline.
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print(" FlowDelta: Instrumenting the checkout pipeline")
print("=" * 60)

watch_fns = {"checkout", "build_cart", "apply_coupon", "process_payment", "create_order"}

order = fd.observe(
    checkout,
    user_id="user-42",
    product_quantities={"p001": 1, "p002": 2},
    coupon="SAVE10",
    # ------- FlowDelta parameters -------
    flow_id="checkout",
    watch_functions=watch_fns,
    golden=True,
    generate_tests=True,
)

print(f"\n  Order created: {order}")
print("\n  Traces + deltas → .flowdelta/runs/")
print("  Generated tests → generated_tests/test_checkout.py")
print("\n  Run tests with:  pytest generated_tests/")
print("  View dashboard:  flowdelta dashboard")

