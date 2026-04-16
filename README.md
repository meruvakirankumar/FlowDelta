# FlowDelta

**AI-powered application flow identification and delta-based automated test generation.**

FlowDelta combines GenAI, runtime tracing, language server tools, and tree walking to:

1. **Identify** distinct application flows from source code (GenAI + AST)
2. **Record** state changes as each flow executes (sys.settrace / DAP)
3. **Compute** line-by-line deltas between successive states
4. **Generate** pytest test cases from those deltas automatically

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        FlowDelta Pipeline                    │
│                                                             │
│  ┌──────────────┐   ┌──────────────┐   ┌────────────────┐  │
│  │  Phase 1     │   │  Phase 2     │   │  Phase 3       │  │
│  │  Flow ID     │──▶│  State       │──▶│  Delta         │  │
│  │              │   │  Capture     │   │  Engine        │  │
│  │ • tree-sitter│   │              │   │                │  │
│  │   AST walk   │   │ • sys.settrace│   │ • DeepDiff     │  │
│  │ • Call graph │   │ • DAP client │   │ • VariableDelta│  │
│  │ • LLM flow   │   │ • LSP types  │   │ • DeltaStore   │  │
│  │   clustering │   │              │   │   (JSONL/SQLite)│  │
│  └──────────────┘   └──────────────┘   └────────────────┘  │
│                                                 │            │
│                                                ▼            │
│                                      ┌────────────────┐     │
│                                      │  Phase 4       │     │
│                                      │  Test Gen      │     │
│                                      │                │     │
│                                      │ • Assertion    │     │
│                                      │   strategies   │     │
│                                      │ • LLM names    │     │
│                                      │ • Jinja2 render│     │
│                                      └────────────────┘     │
└─────────────────────────────────────────────────────────────┘
```

---

## Quick Start

### 1. Install

```bash
pip install -e .
# or
pip install -r requirements.txt
```

### 2. Run the sample demo

```bash
cd FlowDelta
python examples/sample_app/run_flows.py
```

This will:
- Parse `examples/sample_app/ecommerce.py` with tree-sitter
- Build a call graph of all functions
- Identify flows (heuristic if no OpenAI key; LLM if key set)
- Record a checkout flow execution with `sys.settrace`
- Compute state deltas snapshot-by-snapshot
- Write a `generated_tests/test_checkout.py` pytest file

### 3. Run tests

```bash
pytest tests/
```

### 4. Use the CLI

```bash
# Phase 1: Identify flows
flowdelta analyze src/ --output flows.json

# Phase 2: Record a trace (mark as golden baseline)
flowdelta record checkout examples/sample_app/ecommerce.py --golden

# Phase 3: Compute deltas for a recorded run
flowdelta diff <run_id>

# Phase 4: Generate pytest file
flowdelta generate checkout

# Full pipeline in one shot
flowdelta run src/ examples/sample_app/ecommerce.py

# Compare a new run to the golden baseline
flowdelta compare checkout <run_id>
```

---

## Configuration

Edit `config/config.yaml`:

```yaml
llm:
  provider: openai
  model: gpt-4o
  api_key_env: OPENAI_API_KEY   # set this env var

state_tracker:
  backend: systrace             # or dap (for external processes)
  capture:
    line_level: false           # true = capture every line (verbose)
    max_depth: 4

delta_engine:
  store_path: .flowdelta/runs
  format: jsonl

test_generator:
  output_dir: generated_tests
  llm_augment: true
```

---

## How It Works

### Phase 1 – Flow Identification

```
Source Code
    │
    ▼
tree-sitter AST
    │  ← FunctionDef, CallEdge, imports
    ▼
NetworkX Call Graph
    │  ← nodes = functions, edges = calls
    ▼
LLM (GPT-4o)
    │  ← "cluster these functions into named flows"
    ▼
FlowMap: [checkout, user-registration, order-tracking, ...]
```

The LLM receives the call graph in JSON and returns structured `Flow` objects,
each with an entry function, ordered steps, and suggested breakpoints.

**Heuristic fallback** (no API key): each zero-in-degree function becomes
its own flow — works well for small codebases.

---

### Phase 2 – State Capture

**`sys.settrace` backend** (default, in-process, no setup):

```python
recorder = SysTraceRecorder(watch_functions={"checkout", "build_cart"})
recorder.record(checkout, user_id="u1", product_quantities={"p001": 1})
# → List[StateSnapshot]  one per call/return/line event
```

Each `StateSnapshot` contains:
- `file`, `line`, `function` — where execution paused
- `locals` — serialized deep copy of local variable scope
- `sequence` — monotonic hit counter

**DAP backend** (for black-box / compiled applications):

```bash
python -m debugpy --listen 5678 --wait-for-client myapp.py
```
```python
async with DAPClient("127.0.0.1", 5678) as client:
    await client.initialize()
    await client.set_breakpoints("myapp.py", [10, 25, 42])
    await client.launch("myapp.py")
    async for snapshot in client.iter_breakpoint_hits():
        process(snapshot)
```

**LSP enrichment** (optional): connects to `pylsp` or `pyright` to annotate
captured variables with inferred type information.

---

### Phase 3 – Delta Engine

For each consecutive pair of snapshots in a trace:

```
Snapshot[i-1].locals  ──┐
                         ├─▶  DeepDiff  ──▶  [VariableDelta, ...]
Snapshot[i].locals    ──┘
```

Each `VariableDelta` records:
- `name` — top-level variable
- `change_type` — `changed | added | removed | type_changed`
- `old_value`, `new_value`
- `deep_path` — full path into nested structures (e.g. `root['cart']['items'][0]`)

The full `TraceDelta` timeline is stored in `.flowdelta/runs/deltas.jsonl`.

#### Golden runs & regression detection

```python
store.save_trace(trace, golden=True)   # record baseline

# Later, after a code change:
store.compare_to_golden(new_delta)
# → { "regression_count": 2, "new_failures": [...], "resolved": [...] }
```

---

### Phase 4 – Test Generation

Each `VariableDelta` → one or more assertion strategies:

| Change type    | Strategy                                 | Priority |
|----------------|------------------------------------------|----------|
| bool changed   | `assert var == True/False`               | 1        |
| int/float      | `assert var == expected`                 | 2        |
| int direction  | `assert var > old_value`                 | 3        |
| str changed    | `assert var == "expected"`               | 2        |
| list length    | `assert len(var) == N`                   | 2        |
| None           | `assert var is None`                     | 2        |
| type changed   | `assert isinstance(var, NewType)`        | 2        |
| added          | `assert var is not None`                 | 2        |

The LLM (`LLMTestWriter`) then adds human-readable function names,
docstrings, and up to 2 additional edge-case assertions per group.

Finally, `TestRenderer` renders everything through a Jinja2 template into
a ready-to-run `generated_tests/test_<flow_id>.py`.

---

## Sprint 2 – Integrations

### DAPLauncher – managed debugpy subprocess

`DAPLauncher` wraps the full `debugpy` lifecycle so you never need to start the server manually:

```python
from src.state_tracker import DAPLauncher

# Script mode: spawn debugpy, set breakpoints, collect snapshots
async with DAPLauncher(
    "src/app.py",
    breakpoints={"src/app.py": [18, 35, 60]},
) as client:
    await client.configuration_done()
    async for snapshot in client.iter_breakpoint_hits():
        process(snapshot)

# Attach mode: connect to an already-running debugpy server
async with DAPLauncher.attach(pid=12345, port=5678) as client:
    ...

# One-liner helper
snapshots = await DAPLauncher.run_and_capture(
    "src/app.py",
    breakpoints={"src/app.py": [10, 42]},
)
```

Internals: starts `python -m debugpy --listen HOST:PORT --wait-for-client SCRIPT`,
polls the TCP port with a configurable timeout, then hands off to `DAPClient`.

---

### LSPAnnotator – type-aware state capture

`LSPAnnotator` enriches each `StateSnapshot` with inferred type information
from a running language server (pylsp or pyright):

```python
from src.state_tracker import LSPClient, LSPAnnotator

async with LSPClient(root_path=".", server="pylsp") as lsp:
    annotator = LSPAnnotator(lsp)

    # Annotate all snapshots in a trace (opens files once, caches source)
    type_maps = await annotator.annotate_trace(trace)
    # → [{"cart": "Cart", "user_id": "str", "qty": "int"}, ...]

    # Or annotate a single snapshot
    types = await annotator.annotate(snapshot)
```

The annotator scans backward from the snapshot's line to find each variable,
calls `textDocument/hover` at that column, and parses the type string.
Falls back to Python `type()` introspection when LSP cannot resolve a symbol.

This enables the test generator to produce richer assertions:

```python
assert isinstance(result['cart'], Cart)      # from LSP type
assert result['qty'] >= 0                    # from numeric type
```

---

### JavaScript / TypeScript support end-to-end

`ASTAnalyzer` fully supports `.js` / `.mjs` / `.cjs` files (TypeScript
support is available when `tree-sitter-typescript` is installed).

The sample application is now available in both languages:

| File | Language |
|---|---|
| [examples/sample_app/ecommerce.py](examples/sample_app/ecommerce.py) | Python |
| [examples/sample_app/ecommerce.js](examples/sample_app/ecommerce.js) | JavaScript (ES2020) |

Analyze a JS project exactly the same way:

```bash
flowdelta analyze src/frontend/ --output js_flows.json
```

The `CallGraphBuilder` and `LLMFlowMapper` are language-agnostic — they
operate on the `ASTAnalysis` data model regardless of source language.

---

### SQLiteQueryAPI – rich delta history queries

When `format="sqlite"` is configured, `SQLiteQueryAPI` gives you eight
query methods over stored run history:

```python
from src.delta_engine import SQLiteQueryAPI

with SQLiteQueryAPI(store_path=".flowdelta/runs") as api:

    # What flows exist and how often do they run?
    api.flows_summary()
    # → [{"flow_id": "checkout", "total_runs": 12, "golden_runs": 1, ...}]

    # Timeline of runs for a flow
    api.run_history("checkout", limit=20)

    # Which variables change most? (identify flaky state)
    api.hot_variables("checkout", limit=5)
    # → [{"variable": "total", "change_count": 36, "change_types": {...}}]

    # Trend of change volume across runs (for charting)
    api.regression_trend("checkout")

    # Find all runs where a specific variable changed
    api.search_changes("status", flow_id="checkout", change_type="changed")

    # Side-by-side comparison of two runs
    api.compare_runs(run_id_a="abc12345", run_id_b="def67890")
    # → {"only_in_a": [...], "only_in_b": [...], "in_both": [...]}

    # Delete a run and reclaim space
    api.delete_run("old-run-id")
    api.vacuum()
```

---

## Project Structure

```
FlowDelta/
├── src/
│   ├── flow_identifier/
│   │   ├── ast_analyzer.py       # tree-sitter AST parsing (Python + JS/TS)
│   │   ├── call_graph.py         # NetworkX call graph builder
│   │   └── llm_flow_mapper.py    # LLM flow clustering
│   ├── state_tracker/
│   │   ├── dap_client.py         # asyncio DAP client (debugpy)
│   │   ├── dap_launcher.py       # ★ Sprint 2: managed debugpy subprocess
│   │   ├── lsp_client.py         # LSP stdio client (pylsp/pyright)
│   │   ├── lsp_annotator.py      # ★ Sprint 2: type annotation for snapshots
│   │   └── trace_recorder.py     # sys.settrace + DAP recorders
│   ├── delta_engine/
│   │   ├── state_diff.py         # DeepDiff wrapper → VariableDelta
│   │   ├── delta_store.py        # JSONL / SQLite persistence
│   │   └── sqlite_query.py       # ★ Sprint 2: rich SQL query API
│   ├── test_generator/
│   │   ├── assertion_gen.py      # delta → assertion strategies
│   │   ├── llm_test_writer.py    # LLM names + docstrings
│   │   └── test_renderer.py      # Jinja2 → .py file
│   └── orchestrator.py           # CLI (click) + FlowDeltaPipeline
├── config/config.yaml
├── templates/test_module.py.j2
├── examples/sample_app/
│   ├── ecommerce.py              # 3-flow sample application (Python)
│   ├── ecommerce.js              # ★ Sprint 2: same app in JavaScript
│   └── run_flows.py              # end-to-end demo script
├── tests/
│   ├── test_ast_analyzer.py
│   ├── test_delta_engine.py
│   ├── test_assertion_gen.py
│   ├── test_dap_launcher.py      # ★ Sprint 2
│   ├── test_lsp_annotator.py     # ★ Sprint 2
│   ├── test_js_flow.py           # ★ Sprint 2
│   └── test_sqlite_query.py      # ★ Sprint 2
├── generated_tests/              # output of Phase 4 (gitignored)
├── .flowdelta/runs/              # trace + delta storage (gitignored)
├── pyproject.toml
└── requirements.txt
```

---

## Action Plan (Phased Rollout)

### Sprint 1 – Core Infrastructure ✅
- [x] tree-sitter AST analyzer (Python + JavaScript)
- [x] NetworkX call graph builder
- [x] LLM flow mapper with heuristic fallback
- [x] `sys.settrace` recorder with deep serialization
- [x] DeepDiff-based delta engine
- [x] JSONL delta store

### Sprint 2 – Integrations ✅
- [x] `DAPLauncher` – managed `debugpy` subprocess lifecycle with attach mode
- [x] `LSPAnnotator` – type annotation for captured variables via pylsp/pyright
- [x] JavaScript end-to-end: JS sample app + `ASTAnalyzer` + call graph + flow identification
- [x] `SQLiteQueryAPI` – 8 rich SQL query methods (summary, history, hot vars, trend, search, compare, delete, vacuum)
- [x] Full test coverage for all Sprint 2 components (mocked + integration)

### Sprint 3 – Test Quality ✅
- [x] Invariant detection (variables that should never change)
- [x] Property-based test generation (Hypothesis integration)
- [x] Mutation testing feedback loop
- [x] CI/CD integration (GitHub Actions example)

### Sprint 4 – Scale & Observability ✅
- [x] OpenTelemetry trace export
- [x] Web dashboard for delta visualization
- [x] Multi-run regression trend charts
- [x] Support for Java (LSP4J) and C# (OmniSharp) via DAP

---

## License

MIT
