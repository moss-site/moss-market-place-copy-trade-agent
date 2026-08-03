import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config as cfg


def setup_logger(name: str = "follow_agent") -> logging.Logger:
    log_dir = Path(cfg.get("log_dir", str(cfg.get_instance_dir() / "logs"))).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fmt.default_msec_format = "%s.%03d"

    # Rotating file: 10 MB × 5 backups
    fh = RotatingFileHandler(
        log_dir / "service.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger
