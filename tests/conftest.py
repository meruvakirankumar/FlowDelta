"""
pytest configuration for the FlowDelta test suite.

Ensures the project root is on sys.path so every test file can do
``from src.xxx import ...`` without repeating the path-manipulation boilerplate.
"""
import sys
from pathlib import Path

# Project root (parent of this tests/ directory)
_ROOT = Path(__file__).parent.parent

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Make tests/ itself importable so ``from helpers import ...`` works
_TESTS = Path(__file__).parent

if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))
