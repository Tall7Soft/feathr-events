"""Structured-logging setup.

Logs go to stdout as one JSON object per line so the platform's log
pipeline can ingest them without an additional shipper. Called once
from :func:`app.lifecycle.lifespan` at startup.
"""

import logging
import sys

from pythonjsonlogger import jsonlogger


def configure_logging(level: str = "INFO") -> None:
    """Install a single JSON stdout handler on the root logger.

    Replaces any existing handlers (so re-running the lifespan in
    tests does not stack duplicates) and tones down the noisy clients
    (``uvicorn.access``, ``elastic_transport``) to WARNING.
    """
    root = logging.getLogger()
    if root.handlers:
        root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "ts", "levelname": "level"},
        )
    )
    root.addHandler(handler)
    root.setLevel(level.upper())

    for noisy in ("uvicorn.access", "elastic_transport.transport"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
