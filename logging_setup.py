import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    rotating_file = RotatingFileHandler(
        log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    rotating_file.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[console, rotating_file], force=True)
