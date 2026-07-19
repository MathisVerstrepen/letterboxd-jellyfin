import contextvars
import json
import logging
import os
import re
import sys
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Iterator, Mapping


CONTEXT_FIELDS = (
    "run_id",
    "user",
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
    "attempted",
    "succeeded",
    "skipped_items",
)

_IDENTITY_FIELDS = ("run_id", "user")
_LOG_CONTEXT: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "log_context", default=MappingProxyType({})
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|apikey|authorization|cookie|password|token|secret)\b"
    r"\s*[:=]\s*)(?:bearer\s+)?[^\s,;&]+"
)
_URL_USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@")


def _redact_string(value: str) -> str:
    value = _URL_USERINFO.sub(r"\1[REDACTED]@", value)
    return _CREDENTIAL_ASSIGNMENT.sub(r"\1[REDACTED]", value)


def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        return {_sanitize(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return value


class ContextLoggerAdapter(logging.LoggerAdapter):
    """Bind a component and merge the active run/user context per record."""

    def process(self, msg: Any, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        extra = dict(kwargs.get("extra") or {})
        active_context = _LOG_CONTEXT.get()
        for field in _IDENTITY_FIELDS:
            if field in active_context:
                extra[field] = active_context[field]
        extra["component"] = self.extra["component"]
        kwargs["extra"] = extra
        return msg, kwargs


def get_logger(component: str) -> logging.LoggerAdapter:
    """Return the service logger with an immutable component binding."""
    return ContextLoggerAdapter(
        logging.getLogger("letterboxd-sync"),
        MappingProxyType({"component": component}),
    )


@contextmanager
def log_context(**values: Any) -> Iterator[None]:
    """Temporarily add run/user identity values to the current context."""
    unknown = set(values) - set(_IDENTITY_FIELDS)
    if unknown:
        raise ValueError(f"Unsupported log context fields: {sorted(unknown)}")
    updated = dict(_LOG_CONTEXT.get())
    updated.update({key: value for key, value in values.items() if value is not None})
    token = _LOG_CONTEXT.set(MappingProxyType(updated))
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


def new_run_id() -> str:
    """Return a canonical UUID4 string for log correlation."""
    return str(uuid.uuid4())


class JsonFormatter(logging.Formatter):
    """Serialize records to the service's secret-safe JSON log envelope."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, timezone.utc)
        payload = {
            "timestamp": timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "event": _sanitize(getattr(record, "event", "runtime_log")),
            "message": _sanitize(record.getMessage()),
            "component": _sanitize(getattr(record, "component", "unknown")),
        }
        for field in CONTEXT_FIELDS:
            if hasattr(record, field):
                payload[field] = _sanitize(getattr(record, field))

        if record.exc_info:
            exception_type = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            frames = [
                {
                    "file": os.path.basename(frame.filename),
                    "function": frame.name,
                    "line": frame.lineno,
                }
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
            get_logger("logging").warning(
                "Invalid log level; using INFO",
                extra={"event": "log_level_invalid"},
            )

    return logger
