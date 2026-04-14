"""
Test Renderer – Phase 4 of FlowDelta.

Renders a :class:`TestSpec` (with optional LLM metadata) into a real pytest
``.py`` file using a Jinja2 template.

The rendered file is immediately runnable with ``pytest``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .assertion_gen import TestSpec


class TestRenderer:
    """
    Renders a :class:`TestSpec` to a pytest file.

    Parameters
    ----------
    template_dir : str | Path
        Directory containing ``test_module.py.j2``.
    output_dir : str | Path
        Where generated test files are written.
    """

    def __init__(
        self,
        template_dir: str | Path = "templates",
        output_dir: str | Path = "generated_tests",
    ) -> None:
        self.template_dir = Path(template_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(self, spec: TestSpec, filename: Optional[str] = None) -> Path:
        """
        Render *spec* to a file and return the output path.

        Parameters
        ----------
        filename : str | None
            Output filename (default: ``test_<flow_id>.py``).
        """
        template = self._env.get_template("test_module.py.j2")
        content = template.render(
            spec=spec,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

        safe_name = spec.flow_id.replace("-", "_").replace(" ", "_")
        out_name = filename or f"test_{safe_name}.py"
        out_path = self.output_dir / out_name
        out_path.write_text(content, encoding="utf-8")
        return out_path
