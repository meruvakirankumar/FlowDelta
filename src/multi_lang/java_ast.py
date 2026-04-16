"""Java regex-based AST analyzer."""

from __future__ import annotations

import re

from .ast_base import RegexASTAnalyzer


class JavaASTAnalyzer(RegexASTAnalyzer):
    """Lightweight regex-based Java method extractor."""

    _METHOD_RE = re.compile(
        r"(?:public|private|protected|static|\s)*"
        r"[\w<>\[\]]+\s+(\w+)\s*\([^)]*\)\s*(?:throws\s+\S+)?\s*\{",
        re.MULTILINE,
    )
    _LANGUAGE = "java"
    _KEYWORD_BLACKLIST = {"if", "while", "for", "switch", "return"}
