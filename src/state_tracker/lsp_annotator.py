"""
LSP Annotator – Sprint 2 of FlowDelta.

Enriches captured :class:`StateSnapshot` objects with inferred type
information from a running Language Server.

For each variable in a snapshot's ``locals`` dict the annotator:

1. Loads the source file (cached per file).
2. Searches backwards from the snapshot line to find the nearest line
   where the variable name appears as an identifier.
3. Issues a ``textDocument/hover`` request at that column.
4. Parses the hover response for a type string.

The result is a ``types`` dict parallel to ``locals``::

    {
      "cart":    "Cart",
      "user_id": "str",
      "qty":     "int",
    }

This is attached to the snapshot as ``snapshot.types`` (a dynamic attribute)
so downstream consumers can generate richer assertions (e.g.
``assert isinstance(result['cart'], Cart)``).

Typical usage (inside an async context with an active LSPClient)::

    async with LSPClient(root_path=".", server="pylsp") as lsp:
        annotator = LSPAnnotator(lsp)
        await annotator.open_trace_files(trace)
        for snapshot in trace.snapshots:
            snapshot.types = await annotator.annotate(snapshot)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from .lsp_client import LSPClient
from .dap_client import StateSnapshot
from .trace_recorder import FlowTrace

logger = logging.getLogger(__name__)

# Regex that matches a bare identifier (not part of a larger word)
_IDENT_RE = re.compile(r"\b{name}\b")


class LSPAnnotator:
    """
    Annotates variable locals in :class:`StateSnapshot` objects with
    inferred type information from an :class:`LSPClient`.

    Parameters
    ----------
    lsp : LSPClient
        A started, initialized LSP client.
    search_window : int
        Number of lines *above* the snapshot line to scan when looking for
        a variable name (handles multi-line assignments).  Default: 10.
    skip_builtins : bool
        If ``True``, skip variables whose names are Python builtins
        (``True``, ``None``, ``False``, ``__builtins__``, etc.).
    """

    _BUILTINS = frozenset({
        "True", "False", "None", "__builtins__", "__doc__",
        "__name__", "__package__", "__spec__", "__loader__",
        "__file__", "__cached__",
    })

    def __init__(
        self,
        lsp: LSPClient,
        search_window: int = 10,
        skip_builtins: bool = True,
    ) -> None:
        self._lsp = lsp
        self.search_window = search_window
        self.skip_builtins = skip_builtins
        self._source_cache: Dict[str, List[str]] = {}   # filepath → lines

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def open_trace_files(self, trace: FlowTrace) -> None:
        """
        Pre-open all unique source files in *trace* with the LSP server.
        Call this once before annotating individual snapshots for efficiency.
        """
        seen: set = set()
        for snap in trace.snapshots:
            if snap.file and snap.file not in seen and Path(snap.file).exists():
                try:
                    await self._lsp.open_document(snap.file)
                    seen.add(snap.file)
                    logger.debug("Opened %s in LSP", snap.file)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Could not open %s: %s", snap.file, exc)

    async def annotate(self, snapshot: StateSnapshot) -> Dict[str, Optional[str]]:
        """
        Return a ``{var_name: type_str}`` dict for all locals in *snapshot*.
        Variables whose type cannot be resolved have value ``None``.
        """
        if not snapshot.file or not Path(snapshot.file).exists():
            return {}

        source_lines = self._load_source(snapshot.file)
        types: Dict[str, Optional[str]] = {}

        for var_name, value in snapshot.locals.items():
            if self.skip_builtins and var_name in self._BUILTINS:
                continue
            col = self._find_column(source_lines, snapshot.line, var_name)
            if col is None:
                # Fall back to runtime type of the captured value
                types[var_name] = self._runtime_type(value)
                continue
            try:
                type_str = await self._lsp.type_at(snapshot.file, snapshot.line, col)
                types[var_name] = type_str
            except Exception as exc:  # noqa: BLE001
                logger.debug("LSP hover failed for %s at %s:%s: %s",
                             var_name, snapshot.file, snapshot.line, exc)
                types[var_name] = self._runtime_type(value)

        return types

    async def annotate_trace(self, trace: FlowTrace) -> List[Dict[str, Optional[str]]]:
        """
        Annotate every snapshot in *trace* and return a list of type dicts
        (same order as ``trace.snapshots``).

        Also attaches results as ``snapshot.types`` for in-place enrichment.
        """
        await self.open_trace_files(trace)
        results: List[Dict[str, Optional[str]]] = []
        for snap in trace.snapshots:
            types = await self.annotate(snap)
            snap.types = types          # type: ignore[attr-defined]
            results.append(types)
        return results

    # ------------------------------------------------------------------
    # Source scanning helpers
    # ------------------------------------------------------------------

    def _load_source(self, filepath: str) -> List[str]:
        if filepath not in self._source_cache:
            try:
                self._source_cache[filepath] = Path(filepath).read_text(
                    encoding="utf-8"
                ).splitlines()
            except OSError:
                self._source_cache[filepath] = []
        return self._source_cache[filepath]

    def _find_column(
        self,
        lines: List[str],
        snap_line: int,       # 1-based
        var_name: str,
    ) -> Optional[int]:
        """
        Search backward from *snap_line* (inclusive) within *search_window*
        lines for *var_name* as a standalone identifier.

        Returns the 1-based column of the first match, or ``None``.
        """
        pattern = _IDENT_RE.pattern.format(name=re.escape(var_name))
        compiled = re.compile(pattern)

        start = max(0, snap_line - self.search_window - 1)
        # Walk from the snapshot line upward
        for line_idx in range(snap_line - 1, start - 1, -1):
            if line_idx >= len(lines):
                continue
            line_text = lines[line_idx]
            m = compiled.search(line_text)
            if m:
                return m.start() + 1   # 1-based column

        return None

    # ------------------------------------------------------------------
    # Runtime type fallback
    # ------------------------------------------------------------------

    @staticmethod
    def _runtime_type(value: object) -> str:
        """Return a Python type annotation string from a captured value."""
        if value is None:
            return "None"
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
        if isinstance(value, str):
            return "str"
        if isinstance(value, list):
            return "list"
        # Serialized objects (dicts with __type__) must be checked before plain dict
        if isinstance(value, dict) and "__type__" in value:
            return str(value["__type__"])
        if isinstance(value, dict):
            return "dict"
        if isinstance(value, tuple):
            return "tuple"
        if isinstance(value, set):
            return "set"
        return type(value).__name__
