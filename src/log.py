"""Central logging setup.

Entry points (CLI, Streamlit UI) call `configure()` once; every module gets a
logger via `get_logger(__name__)`. Loggers live under the "vet" namespace so we
never fight Streamlit's or a library's root-logger config.
"""

from __future__ import annotations

import logging
import sys

_ROOT = "vet"
_configured = False


def configure(level: int = logging.INFO) -> None:
    """Attach a single stdout handler to the app's logger namespace. Idempotent."""
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
    )
    root = logging.getLogger(_ROOT)
    root.setLevel(level)
    root.addHandler(handler)
    root.propagate = False
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """A logger under the app namespace. `name` is usually __name__."""
    short = name.split(".")[-1]
    return logging.getLogger(f"{_ROOT}.{short}")
