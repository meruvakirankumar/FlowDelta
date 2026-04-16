"""
Trace Recorder – Phase 2 of FlowDelta.

Orchestrates state capture for a given :class:`Flow`.  Supports two backends:

1. **sys.settrace** (default, zero-dependency)
   Hooks Python's built-in tracing mechanism.  Works in-process.
   Fastest and most portable — no subprocess needed.

2. **DAP** (``backend="dap"``)
   Attaches to an external ``debugpy`` server.  Supports any language
   with a DAP-compliant debug adapter.

In both modes the output is a list of :class:`StateSnapshot` objects
forming a *trace* — the ordered sequence of state captures as the
application flow executes.
"""

from __future__ import annotations

import copy
import importlib
import inspect
import logging
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from .dap_client import StateSnapshot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trace result
# ---------------------------------------------------------------------------

@dataclass
class FlowTrace:
    """
    Complete trace of one execution of a :class:`Flow`.

    Attributes
    ----------
    flow_id : str
        ID of the flow this trace belongs to.
    run_id : str
        Unique ID for this particular execution (e.g. timestamp + uuid).
    snapshots : list[StateSnapshot]
        Ordered state captures from entry to exit.
    """
    flow_id: str
    run_id: str
    snapshots: List[StateSnapshot] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "flow_id": self.flow_id,
            "run_id": self.run_id,
            "snapshots": [s.to_dict() for s in self.snapshots],
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# sys.settrace recorder
# ---------------------------------------------------------------------------

class SysTraceRecorder:
    """
    In-process tracer using :func:`sys.settrace`.

    Captures local variable state at the *call* and *return* events of
    any function whose qualified name is listed in *watch_functions*.
    Also captures at every *line* event inside watched functions when
    *line_level* is ``True``.

    Parameters
    ----------
    watch_functions : set[str]
        Qualified function names to watch (e.g. ``{"checkout", "Cart.add_item"}``).
    watch_files : set[str] | None
        If provided, only trace events from these file paths.
    line_level : bool
        If ``True``, capture state at every line inside watched functions.
        Produces many more snapshots but gives fine-grained delta data.
    max_depth : int
        Maximum depth for serializing mutable objects (lists, dicts).
    skip_private : bool
        Skip variables whose names start with ``_``.
    """

    def __init__(
        self,
        watch_functions: Set[str],
        watch_files: Optional[Set[str]] = None,
        line_level: bool = False,
        max_depth: int = 4,
        skip_private: bool = True,
    ) -> None:
        self.watch_functions = watch_functions
        self.watch_files = {str(Path(f).resolve()) for f in watch_files} if watch_files else None
        self.line_level = line_level
        self.max_depth = max_depth
        self.skip_private = skip_private
        self._snapshots: List[StateSnapshot] = []
        self._seq = 0
        self._active_frames: Set[int] = set()   # id(frame) for watched frames

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, callable_: Callable, *args: Any, **kwargs: Any) -> Any:
        """
        Execute *callable_* with tracing enabled and return its result.
        Snapshots are collected in ``self.snapshots``.
        """
        self._snapshots.clear()
        self._seq = 0
        self._active_frames.clear()
        sys.settrace(self._global_trace)
        try:
            return callable_(*args, **kwargs)
        finally:
            sys.settrace(None)

    @property
    def snapshots(self) -> List[StateSnapshot]:
        return list(self._snapshots)

    # ------------------------------------------------------------------
    # Trace hooks
    # ------------------------------------------------------------------

    def _global_trace(self, frame: types.FrameType, event: str, arg: Any) -> Optional[Callable]:
        """Called by Python for every new scope."""
        if not self._should_watch_frame(frame):
            return None
        self._active_frames.add(id(frame))
        if event == "call":
            self._capture(frame, event="call")
        return self._local_trace

    def _local_trace(self, frame: types.FrameType, event: str, arg: Any) -> Optional[Callable]:
        """Called by Python for each event inside a watched scope."""
        if event == "line" and self.line_level:
            self._capture(frame, event="line")
        elif event == "return":
            self._capture(frame, event="return")
            self._active_frames.discard(id(frame))
        elif event == "exception":
            self._capture(frame, event="exception")
        return self._local_trace

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _should_watch_frame(self, frame: types.FrameType) -> bool:
        filename = str(Path(frame.f_code.co_filename).resolve())
        if self.watch_files and filename not in self.watch_files:
            return False
        fn_name = frame.f_code.co_qualname  # Python 3.3+

        # Direct match
        if fn_name in self.watch_functions:
            return True
        # Suffix match: "Cart.add_item" matches "add_item"
        simple_name = fn_name.split(".")[-1]
        return simple_name in self.watch_functions

    def _capture(self, frame: types.FrameType, event: str) -> None:
        locals_raw = dict(frame.f_locals)   # dict() handles FrameLocalsProxy (Python 3.13+)
        serialized = self._serialize(locals_raw, depth=0)
        self._seq += 1
        snapshot = StateSnapshot(
            event=event,
            thread_id=0,
            file=frame.f_code.co_filename,
            line=frame.f_lineno,
            function=frame.f_code.co_qualname,
            locals=serialized,
            sequence=self._seq,
        )
        self._snapshots.append(snapshot)

    def _serialize(self, obj: Any, depth: int) -> Any:
        """Recursively serialize *obj* to a JSON-safe value."""
        if depth > self.max_depth:
            return f"<depth limit: {type(obj).__name__}>"
        if obj is None or isinstance(obj, (bool, int, float, str)):
            return obj
        if isinstance(obj, (bytes, bytearray)):
            return obj.hex()
        if isinstance(obj, dict):
            return {
                str(k): self._serialize(v, depth + 1)
                for k, v in obj.items()
                if not (self.skip_private and isinstance(k, str) and k.startswith("_"))
            }
        if isinstance(obj, (list, tuple, set, frozenset)):
            items = [self._serialize(i, depth + 1) for i in obj]
            return items
        # For objects, serialize their public __dict__
        if hasattr(obj, "__dict__"):
            d = {
                k: self._serialize(v, depth + 1)
                for k, v in vars(obj).items()
                if not (self.skip_private and k.startswith("_"))
            }
            d["__type__"] = type(obj).__qualname__
            return d
        return repr(obj)


# ---------------------------------------------------------------------------
# DAP-based recorder (async, wraps DAPClient)
# ---------------------------------------------------------------------------

class DAPTraceRecorder:
    """
    Async recorder that drives a :class:`DAPClient` to capture state.

    Typical usage (inside an async context)::

        recorder = DAPTraceRecorder(host="127.0.0.1", port=5678)
        trace = await recorder.record_flow(
            flow_id="checkout",
            program="examples/sample_app/ecommerce.py",
            breakpoint_lines={"examples/sample_app/ecommerce.py": [18, 35, 60]},
        )
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 5678) -> None:
        self.host = host
        self.port = port

    async def record_flow(
        self,
        flow_id: str,
        run_id: str,
        program: str,
        breakpoint_lines: Dict[str, List[int]],
    ) -> FlowTrace:
        from .dap_client import DAPClient  # avoid circular at module level
        import uuid

        trace = FlowTrace(flow_id=flow_id, run_id=run_id)
        try:
            async with DAPClient(self.host, self.port) as client:
                await client.initialize()
                for filepath, lines in breakpoint_lines.items():
                    await client.set_breakpoints(filepath, lines)
                await client.launch(program)
                await client.configuration_done()
                async for snapshot in client.iter_breakpoint_hits():
                    trace.snapshots.append(snapshot)
        except Exception as exc:  # noqa: BLE001
            trace.error = str(exc)
            logger.error("DAPTraceRecorder error: %s", exc)
        return trace
