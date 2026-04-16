"""Shared DAP variable normalization for all languages."""

from __future__ import annotations

from typing import Any, Dict, Set


def normalize_dap_variables(
    raw: Dict[str, Any],
    int_types: Set[str],
    float_types: Set[str],
    bool_types: Set[str],
    null_types: Set[str],
    string_types: Set[str],
) -> Dict[str, Any]:
    """
    Convert a DAP ``variables`` response into FlowDelta's
    ``Dict[str, Any]`` locals schema.

    Type sets are language-specific (e.g. Java uses ``"boolean"``,
    C# uses ``"bool"``/``"Boolean"``).
    """
    result: Dict[str, Any] = {}
    for var in raw.get("variables", []):
        name = var.get("name", "")
        type_ = var.get("type", "")
        value_str = var.get("value", "")

        if type_ in int_types:
            try:
                result[name] = int(value_str)
                continue
            except ValueError:
                pass
        if type_ in float_types:
            try:
                result[name] = float(value_str)
                continue
            except ValueError:
                pass
        if type_ in bool_types:
            result[name] = value_str.lower() == "true"
            continue
        if type_ in null_types or value_str == "null":
            result[name] = None
            continue
        if type_ in string_types:
            result[name] = value_str.strip('"')
            continue
        result[name] = value_str

    return result
