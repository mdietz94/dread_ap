"""Logging configuration for DreadClient. Lifted from smo_archipelago."""

from __future__ import annotations

import logging
import sys


def setup(level: str = "INFO") -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level.upper())
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s",
                          datefmt="%H:%M:%S")
    )
    root.addHandler(handler)
