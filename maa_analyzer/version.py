"""Runtime version for source checkouts and development builds.

CI release packages overwrite this file with the tag version and additionally
ship ``build-info.json``; the updater treats ``build-info.json`` as authoritative
for packaged installs.
"""
from __future__ import annotations

__version__ = "0.3.0-dev"

__all__ = ["__version__"]
