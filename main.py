#!/usr/bin/env python3
"""Stable launcher for OnebotMaaLogAnalyzer.

The real bare OneBot application lives in :mod:`maa_analyzer.bare_entry`.
This launcher never imports the analyzer: it selects an immutable version
directory, waits for its startup confirmation, and automatically falls back to
the previous version when a newly installed version fails to start.
"""
from __future__ import annotations

from maa_bootstrap import run_bare_launcher


def _resolve_onebot_mode(cfg: dict, *, debug_enabled: bool) -> str:
    """Backward-compatible import for tests and older integrations."""
    from maa_analyzer.bare_entry import _resolve_onebot_mode as resolve

    return resolve(cfg, debug_enabled=debug_enabled)


if __name__ == "__main__":
    raise SystemExit(run_bare_launcher())
