"""Shared pytest fixtures for R-FINAL bug-fix modules.

Adds the project root (one level above ``tests/``) to ``sys.path`` so the
``auto.*`` package can be imported in tests without installing it.
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
