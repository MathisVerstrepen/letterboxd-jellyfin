import logging
import json
import sys
import traceback
from datetime import datetime, timezone


CONTEXT_FIELDS = (
    "run_id",
    "letterboxd_username",
    "stage",
    "outcome",
    "duration_seconds",
    "failed_items",
    "queue_counts",
    "queue",
    "count",
    "attempt",
    "status_code",
    "path",
    "host",
    "port",
)


class JsonFormatter(logging.Formatter):
    """Serialize records to the service's secret-safe JSON log envelope."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, timezone.utc)
        payload = {
            "timestamp": timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", "runtime_log"),
            "message": record.getMessage(),
        }
        for field in CONTEXT_FIELDS:
            if hasattr(record, field):
                payload[field] = getattr(record, field)

        if record.exc_info:
            exception_type = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            frames = [
                {"file": frame.filename, "function": frame.name, "line": frame.lineno}
                for frame in traceback.extract_tb(record.exc_info[2])
            ]
            payload["exception"] = {"type": exception_type, "frames": frames}

        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def setup_logger(log_level: str | None = None) -> logging.Logger:
    """Configure and return the idempotent service logger."""
    logger = logging.getLogger("letterboxd-sync")
    logger.propagate = False

    # Prevent adding duplicate handlers if this function is called multiple times
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)

    if log_level is not None:
        requested_level = str(log_level).upper()
        valid_level = requested_level in logging._nameToLevel
        logger.setLevel(logging._nameToLevel.get(requested_level, logging.INFO))
        if not valid_level:
            logger.warning(
                "Invalid log level; using INFO",
                extra={"event": "log_level_invalid"},
            )

    return logger
