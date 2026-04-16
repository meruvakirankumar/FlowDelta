"""C# regex-based AST analyzer."""

from __future__ import annotations

import re

from .ast_base import RegexASTAnalyzer


class CSharpASTAnalyzer(RegexASTAnalyzer):
    """Lightweight regex-based C# method extractor."""

    _METHOD_RE = re.compile(
        r"(?:public|private|protected|internal|static|virtual|override|async|\s)*"
        r"[\w<>\[\]?]+\s+(\w+)\s*\([^)]*\)\s*(?:where\s+\S+)?\s*\{",
        re.MULTILINE,
    )
    _LANGUAGE = "csharp"
    _KEYWORD_BLACKLIST = {"if", "while", "for", "foreach", "switch", "using", "catch", "return"}
