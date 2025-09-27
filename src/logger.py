import logging
import sys

def setup_logger(log_level: str = "INFO") -> logging.Logger:
    """Sets up the global logger."""
    logger = logging.getLogger("letterboxd-sync")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Prevent adding duplicate handlers if this function is called multiple times
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger
