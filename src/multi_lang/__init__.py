"""
Multi-Language DAP Support -- Sprint 4 of FlowDelta.

Extends FlowDelta's Debug Adapter Protocol (DAP) backend to support:

* **Java** via LSP4J's built-in DAP server
* **C#** via OmniSharp / ``netcoredbg``

Usage::

    from src.multi_lang import JavaDAPLauncher, CSharpDAPLauncher
    from src.multi_lang import JavaASTAnalyzer, CSharpASTAnalyzer
"""

from .java_launcher import JavaDAPLauncher
from .csharp_launcher import CSharpDAPLauncher
from .java_ast import JavaASTAnalyzer
from .csharp_ast import CSharpASTAnalyzer
from .dap_normalize import normalize_dap_variables

__all__ = [
    "JavaDAPLauncher",
    "CSharpDAPLauncher",
    "JavaASTAnalyzer",
    "CSharpASTAnalyzer",
    "normalize_dap_variables",
]
