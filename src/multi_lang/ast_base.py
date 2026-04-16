"""Base regex-based AST analyzer for brace-delimited languages."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Set


class RegexASTAnalyzer:
    """
    Base class for regex-based method extraction from brace-delimited
    languages (Java, C#, etc.).

    Subclasses set ``_METHOD_RE``, ``_CALL_RE``, ``_LANGUAGE``, and
    ``_KEYWORD_BLACKLIST``.
    """

    _METHOD_RE: re.Pattern
    _CALL_RE: re.Pattern = re.compile(r"(\w+)\s*\(")
    _LANGUAGE: str = "unknown"
    _KEYWORD_BLACKLIST: Set[str] = {"if", "while", "for", "switch", "return"}

    def analyze(self, file_path: str | Path) -> Dict[str, Any]:
        source = Path(file_path).read_text(encoding="utf-8", errors="ignore")
        functions: List[Dict[str, Any]] = []
        for m in self._METHOD_RE.finditer(source):
            name = m.group(1)
            if name in self._KEYWORD_BLACKLIST:
                continue
            line_no = source[: m.start()].count("\n") + 1
            brace_start = m.end()
            depth, end = 1, brace_start
            while end < len(source) and depth > 0:
                if source[end] == "{":
                    depth += 1
                elif source[end] == "}":
                    depth -= 1
                end += 1
            body = source[brace_start:end]
            calls = list({
                c for c in self._CALL_RE.findall(body)
                if c not in self._KEYWORD_BLACKLIST and c != name
            })
            functions.append({
                "name": name,
                "qualified_name": name,
                "file": str(file_path),
                "start_line": line_no,
                "end_line": line_no + body.count("\n"),
                "args": [],
                "calls": calls,
                "language": self._LANGUAGE,
            })
        return {"file": str(file_path), "language": self._LANGUAGE, "functions": functions}
