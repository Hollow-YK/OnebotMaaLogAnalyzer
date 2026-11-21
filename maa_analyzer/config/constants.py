"""Shared configuration constants."""
from __future__ import annotations

CONFIG_VERSION = 3
CONFIG_VERSION_FIELD = "version"

LOG_FORMAT = "%(asctime)s [%(levelname)s]%(config_prefix)s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
BOOTSTRAP_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

ONEBOT_MODES = ("ws", "http_ws", "ws_reverse", "http_ws_reverse")
