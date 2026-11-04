"""
jarvis_logger.py — Centralised logging setup for Jarvis.

Usage:
    from jarvis_logger import get_logger
    log = get_logger(__name__)
    log.info("Hello from Jarvis")
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(level: str = "INFO", log_file: str = "jarvis.log", console: bool = True):
    """Configure the root logger once at startup.

    Args:
        level:    Logging level string ("DEBUG", "INFO", "WARNING", "ERROR").
        log_file: Path to the rotating log file. Pass None or "" to disable file logging.
        console:  Whether to echo logs to stdout as well.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    handlers = []

    if console:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setLevel(numeric_level)
        handlers.append(stream_handler)

    if log_file:
        path = Path(log_file)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent / path
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(numeric_level)
        handlers.append(file_handler)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=numeric_level,
        format=fmt,
        datefmt=datefmt,
        handlers=handlers,
    )


def get_logger(name: str) -> logging.Logger:
    """Return a named logger.  Call setup_logging() once before first use."""
    return logging.getLogger(name)
