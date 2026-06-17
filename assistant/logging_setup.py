"""Logging configuration — console + rotating file. Stdlib only."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_path: str = "./data/assistant.log", level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("assistant")
    if logger.handlers:  # already configured
        return logger
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        fileh = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fileh.setFormatter(fmt)
        logger.addHandler(fileh)
    except OSError:
        logger.warning("Could not open log file at %s; logging to console only.", log_path)

    logger.propagate = False
    return logger


def get_logger(name: str = "assistant") -> logging.Logger:
    return logging.getLogger(name if name.startswith("assistant") else f"assistant.{name}")
