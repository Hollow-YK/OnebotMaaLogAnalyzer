"""GitHub release checking and crash-safe self-update support.

Import concrete modules directly (``maa_analyzer.update.manager`` etc.).  This
package intentionally stays import-light so the release packaging script can
use ``install``/``manifest`` without the runtime dependencies installed.
"""
from __future__ import annotations

__all__: list[str] = []
