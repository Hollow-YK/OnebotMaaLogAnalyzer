#!/usr/bin/env python3
"""Stable AstrBot plugin shim used by CI-built plugin packages.

The shim selects an immutable version directory and exposes its ``Main`` class
to AstrBot.  When a newly installed version cannot even be imported, the shim
falls back to the previous version or the flat plugin shell.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
os.environ.setdefault("MAA_INSTALL_ROOT", str(_ROOT))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from maa_bootstrap import load_astrbot_main  # noqa: E402

Main = load_astrbot_main()

__all__ = ["Main"]
