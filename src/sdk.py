"""
FlowDelta SDK – universal drop-in integration for any Python application.

Quickstart
----------
**Option 1 – instance API**::

    from flowdelta import FlowDelta

    fd = FlowDelta(
        store_path=".flowdelta/runs",
        output_dir="generated_tests",
    )
    result = fd.observe(my_pipeline, arg1, arg2, flow_id="my-pipeline")

**Option 2 – decorator on the entry point**::

    from flowdelta import FlowDelta

    fd = FlowDelta()

    @fd.track(flow_id="data-ingestion", golden=True)
    def ingest(source: str, limit: int) -> list:
        ...

**Option 3 – one-shot call, no instance**::

    from flowdelta import observe

    result = observe(my_function, *args, flow_id="my-flow")

**Option 4 – standalone decorator, no instance**::

    from flowdelta import track

    @track(flow_id="checkout")
    def checkout(user_id: str, cart: dict) -> dict:
        ...

All options produce the same outputs:

* A persisted trace + delta in ``.flowdelta/runs/``
* A runnable pytest file in ``generated_tests/``
* A dashboard-ready dataset (launch with ``flowdelta dashboard``)
"""

from __future__ import annotations

import functools
import inspect
import uuid
from pathlib import Path
from typing import Any, Callable, Optional, Set, Union

# Resolved once at import time so the SDK can locate bundled templates even
# when called from an arbitrary working directory.
_PACKAGE_ROOT = Path(__file__).parent          # .../src/
_BUILTIN_TEMPLATES = _PACKAGE_ROOT.parent / "templates"


class FlowDelta:
    """
    Universal FlowDelta integration handle.

    Provides a high-level API over the four-phase FlowDelta pipeline so that
    any Python project can be instrumented with minimal boilerplate.

    Parameters
    ----------
    src_dir : str | Path, optional
        Source directory of the target application.  Only used when
        :meth:`analyze` is called explicitly.  Not required for
        :meth:`observe` / :meth:`track`.
    config_path : str
        Path to a ``flowdelta.yaml`` or ``config/config.yaml`` file.
        Falls back to built-in defaults when the file does not exist.
    store_path : str
        Directory where traces and deltas are persisted.
        Can be an absolute or relative path.
    output_dir : str
        Directory where generated pytest files are written.
    template_dir : str | Path | None
        Jinja2 template directory.  Defaults to the built-in FlowDelta
        templates — override only if you need custom test scaffolding.

    Examples
    --------
    Minimal integration (works in any project, no config file needed)::

        from flowdelta import FlowDelta

        fd = FlowDelta()
        fd.observe(my_pipeline, *pipeline_args, flow_id="my-pipeline")

    With explicit configuration::

        fd = FlowDelta(
            src_dir="app/",
            config_path="flowdelta.yaml",  # created by `flowdelta init`
            store_path=".flowdelta/runs",
            output_dir="tests/generated",
        )
    """

    def __init__(
        self,
        src_dir: Optional[Union[str, Path]] = None,
        *,
        config_path: str = "config/config.yaml",
        store_path: str = ".flowdelta/runs",
        output_dir: str = "generated_tests",
        template_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        from .orchestrator import FlowDeltaPipeline
        from .delta_engine import DeltaStore
        from .test_generator import TestRenderer

        self.src_dir = Path(src_dir) if src_dir else None

        # Bootstrap pipeline — load_config() returns {} when file is absent,
        # so this works even with no config file at all.
        self._pipeline = FlowDeltaPipeline(config_path=config_path)

        # Override store and renderer with caller-supplied (or default) paths.
        self._pipeline.store = DeltaStore(store_path=store_path)
        self._pipeline.renderer = TestRenderer(
            template_dir=template_dir or _BUILTIN_TEMPLATES,
            output_dir=output_dir,
        )

    # ------------------------------------------------------------------
    # Core API: observe
    # ------------------------------------------------------------------

    def observe(
        self,
        callable_: Callable,
        *args: Any,
        flow_id: Optional[str] = None,
        watch_functions: Optional[Set[str]] = None,
        golden: bool = False,
        generate_tests: bool = True,
        **kwargs: Any,
    ) -> Any:
        """
        Execute *callable_* under FlowDelta tracing and return its result.

        All pipeline phases run automatically:

        1. **Record** — capture state snapshots through ``sys.settrace``
        2. **Diff** — compute state deltas between consecutive snapshots
        3. **Store** — persist trace + delta to the configured store
        4. **Generate** — write a runnable pytest file (skippable)

        Parameters
        ----------
        callable_ :
            Any Python callable — top-level function, method, lambda, etc.
        *args, **kwargs :
            Forwarded verbatim to *callable_*.
        flow_id : str, optional
            Human-readable identifier for this flow.
            Defaults to the callable's ``__name__``.
        watch_functions : set[str], optional
            Names of functions to trace inside the call.  When omitted,
            FlowDelta automatically discovers all public functions defined
            in the same module as *callable_* — the best default for most
            single-module pipelines.

            Pass an explicit set when you want fine-grained control::

                fd.observe(process, watch_functions={"process", "validate", "store"})

        golden : bool
            Mark this run as the golden (reference) baseline for future
            regression comparisons.
        generate_tests : bool
            Write a pytest file from the captured deltas.  ``True`` by
            default; set to ``False`` to skip test generation and just
            record + diff.

        Returns
        -------
        Any
            The return value of *callable_* (unchanged).
        """
        from .state_tracker import SysTraceRecorder, FlowTrace

        fid = flow_id or getattr(callable_, "__name__", "flow")
        watch_fns = (
            watch_functions
            if watch_functions is not None
            else self._auto_watch(callable_)
        )

        recorder = SysTraceRecorder(
            watch_functions=watch_fns,
            line_level=False,
            max_depth=self._pipeline._max_depth,
            skip_private=True,
        )
        run_id = str(uuid.uuid4())[:8]
        result = recorder.record(callable_, *args, **kwargs)

        trace = FlowTrace(
            flow_id=fid,
            run_id=run_id,
            snapshots=recorder.snapshots,
        )
        self._pipeline.store.save_trace(trace, golden=golden)

        td = self._pipeline.diff(trace)

        if generate_tests:
            self._pipeline.generate(td)

        return result

    # ------------------------------------------------------------------
    # Decorator API: track
    # ------------------------------------------------------------------

    def track(
        self,
        fn: Optional[Callable] = None,
        *,
        flow_id: Optional[str] = None,
        watch_functions: Optional[Set[str]] = None,
        golden: bool = False,
        generate_tests: bool = True,
    ):
        """
        Decorator that wraps a function with FlowDelta tracing.

        Works with or without call arguments::

            @fd.track
            def my_function(...): ...

            @fd.track(flow_id="checkout", golden=True)
            def checkout(...): ...

        The original function's return value is always preserved.

        Parameters mirror :meth:`observe` (except *callable_*).
        """
        def decorator(f: Callable) -> Callable:
            @functools.wraps(f)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                return self.observe(
                    f, *args,
                    flow_id=flow_id or f.__name__,
                    watch_functions=watch_functions,
                    golden=golden,
                    generate_tests=generate_tests,
                    **kwargs,
                )
            return wrapper

        if fn is not None:   # @fd.track  — used without parentheses
            return decorator(fn)
        return decorator     # @fd.track(...)  — used with parentheses

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    def dashboard(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        """
        Launch the FlowDelta web dashboard (blocking call).

        Open http://<host>:<port> in a browser to view flow timelines,
        delta reports, and trend charts for all recorded runs.

        Press ``Ctrl+C`` to stop the server.
        """
        from .observability import DeltaDashboard

        dash = DeltaDashboard(self._pipeline.store)
        print(f"FlowDelta Dashboard → http://{host}:{port}  (Ctrl+C to stop)")
        dash.run(host=host, port=port)

    # ------------------------------------------------------------------
    # Optional Phase 1: AST analysis
    # ------------------------------------------------------------------

    def analyze(self, src_dir: Optional[Union[str, Path]] = None):
        """
        Run Phase 1 AST + LLM flow identification on *src_dir*.

        Returns a :class:`FlowMap`.  This is optional — :meth:`observe`
        and :meth:`track` do not require it.  Call it when you want an
        AI-generated map of logical flows before deciding what to trace.

        Parameters
        ----------
        src_dir : str | Path, optional
            Directory to analyze.  Defaults to ``self.src_dir`` or ``"."``.
        """
        return self._pipeline.analyze(str(src_dir or self.src_dir or "."))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _auto_watch(callable_: Callable) -> Set[str]:
        """
        Discover all public function names in the same module as *callable_*.

        This gives FlowDelta a sensible default set of functions to watch when
        the caller did not specify *watch_functions* explicitly.

        Falls back to ``{callable_.__name__}`` when module inspection fails
        (e.g. lambdas, built-ins, dynamically created functions).
        """
        try:
            mod = inspect.getmodule(callable_)
            if mod is not None:
                names = {
                    name
                    for name, obj in inspect.getmembers(mod, inspect.isfunction)
                    if not name.startswith("_")
                }
                if names:
                    return names
        except (TypeError, OSError):
            pass
        return {getattr(callable_, "__name__", "fn")}


# ---------------------------------------------------------------------------
# Module-level convenience API
# ---------------------------------------------------------------------------

def observe(
    callable_: Callable,
    *args: Any,
    flow_id: Optional[str] = None,
    watch_functions: Optional[Set[str]] = None,
    store_path: str = ".flowdelta/runs",
    output_dir: str = "generated_tests",
    golden: bool = False,
    generate_tests: bool = True,
    **kwargs: Any,
) -> Any:
    """
    One-shot FlowDelta trace — no instance needed.

    Creates a temporary :class:`FlowDelta` instance, runs :meth:`observe`,
    and returns the callable's result.  Ideal for scripting, CI pipelines,
    or one-off instrumentation::

        from flowdelta import observe

        result = observe(
            my_pipeline,
            input_data,
            flow_id="data-ingestion",
            golden=True,
        )

    Parameters
    ----------
    callable_ :
        Any Python callable.
    *args, **kwargs :
        Forwarded to *callable_*.
    flow_id : str, optional
        Flow identifier.
    watch_functions : set[str], optional
        Explicit set of function names to trace.
    store_path : str
        Where to persist traces and deltas.
    output_dir : str
        Where to write generated test files.
    golden : bool
        Mark as the golden baseline run.
    generate_tests : bool
        Write pytest file after tracing.

    Returns
    -------
    Any
        The return value of *callable_*.
    """
    fd = FlowDelta(store_path=store_path, output_dir=output_dir)
    return fd.observe(
        callable_, *args,
        flow_id=flow_id,
        watch_functions=watch_functions,
        golden=golden,
        generate_tests=generate_tests,
        **kwargs,
    )


def track(
    fn: Optional[Callable] = None,
    *,
    flow_id: Optional[str] = None,
    watch_functions: Optional[Set[str]] = None,
    store_path: str = ".flowdelta/runs",
    output_dir: str = "generated_tests",
    golden: bool = False,
    generate_tests: bool = True,
):
    """
    Standalone decorator that instruments any function with FlowDelta.

    Works with or without call arguments::

        from flowdelta import track

        @track
        def my_pipeline(data): ...

        @track(flow_id="checkout", golden=True)
        def checkout(user_id: str, cart: dict) -> dict: ...

    Parameters
    ----------
    fn : callable, optional
        The function being decorated (when used without parentheses).
    flow_id : str, optional
        Flow identifier.  Defaults to the function's ``__name__``.
    watch_functions : set[str], optional
        Names of functions to trace inside each call.
    store_path : str
        Where to persist traces and deltas.
    output_dir : str
        Where to write generated test files.
    golden : bool
        Mark the first invocation as the golden baseline run.
    generate_tests : bool
        Write pytest file after each traced call.
    """
    fd = FlowDelta(store_path=store_path, output_dir=output_dir)
    return fd.track(
        fn,
        flow_id=flow_id,
        watch_functions=watch_functions,
        golden=golden,
        generate_tests=generate_tests,
    )
