"""dual_agent — the V2 collaboration engine package.

The engine modules import each other by flat top-level name (they grew up as
a path-based toolkit). This package exposes them for installation by placing
its own directory on ``sys.path`` so that both ``import dual_agent.cli`` and
the internal flat imports keep working unchanged. Pure standard library.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

__version__ = "2.0.0"
