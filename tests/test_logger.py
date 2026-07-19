import io
import json
import logging
import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime

from src.logger import JsonFormatter, get_logger, log_context, new_run_id, setup_logger


class _JsonCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict] = []
        self._records_lock = threading.Lock()
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        payload = json.loads(self.format(record))
        with self._records_lock:
            self.records.append(payload)


class LoggerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base_logger = logging.getLogger("letterboxd-sync")
        self.original_handlers = self.base_logger.handlers[:]
        self.original_level = self.base_logger.level
        self.original_propagate = self.base_logger.propagate
        self.capture = _JsonCapture()
        self.base_logger.handlers = [self.capture]
        self.base_logger.setLevel(logging.DEBUG)
        self.base_logger.propagate = False

    def tearDown(self) -> None:
        self.base_logger.handlers = self.original_handlers
        self.base_logger.setLevel(self.original_level)
        self.base_logger.propagate = self.original_propagate

    def test_required_schema_timestamp_and_allowlist(self) -> None:
        get_logger("sync").info(
            "Completed work",
            extra={"event": "work_completed", "count": 2, "unknown": "drop-me"},
        )

        payload = self.capture.records[-1]
        self.assertEqual(
            {"timestamp", "level", "logger", "event", "message", "component"},
            {"timestamp", "level", "logger", "event", "message", "component"}
            & payload.keys(),
        )
        self.assertEqual("sync", payload["component"])
        for field in ("timestamp", "level", "logger", "event", "message", "component"):
            self.assertIsInstance(payload[field], str)
        self.assertEqual(2, payload["count"])
        self.assertNotIn("unknown", payload)
        self.assertTrue(payload["timestamp"].endswith("Z"))
        datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))

    def test_uuid4_generation(self) -> None:
        value = new_run_id()
        parsed = uuid.UUID(value)
        self.assertEqual(4, parsed.version)
        self.assertEqual(str(parsed), value)

    def test_nested_context_restores_and_prevents_identity_override(self) -> None:
        logger = get_logger("sync")
        with log_context(run_id="outer-run"):
            logger.info(
                "Outer",
                extra={
                    "event": "outer",
                    "run_id": "wrong-run",
                    "component": "wrong-component",
                },
            )
            with log_context(user="alice"):
                logger.info(
                    "Inner",
                    extra={"event": "inner", "run_id": "wrong", "user": "wrong"},
                )
            logger.info("Restored", extra={"event": "restored"})

        try:
            with log_context(run_id="exception-run", user="bob"):
                raise RuntimeError("context exit")
        except RuntimeError:
            pass
        logger.info("Outside", extra={"event": "outside"})

        outer, inner, restored, outside = self.capture.records
        self.assertEqual(("outer-run", "sync"), (outer["run_id"], outer["component"]))
        self.assertEqual(("outer-run", "alice"), (inner["run_id"], inner["user"]))
        self.assertEqual("outer-run", restored["run_id"])
        self.assertNotIn("user", restored)
        self.assertNotIn("run_id", outside)
        self.assertNotIn("user", outside)

    def test_each_executor_submission_receives_its_own_copied_context(self) -> None:
        logger = get_logger("letterboxd")

        def worker(event: str) -> None:
            logger.info("Worker", extra={"event": event})

        with ThreadPoolExecutor(max_workers=2) as executor:
            with log_context(run_id="run-a", user="alice"):
                first = executor.submit(copy_context().run, worker, "worker_a")
            with log_context(run_id="run-b", user="bob"):
                second = executor.submit(copy_context().run, worker, "worker_b")
            first.result()
            second.result()
        logger.info("After workers", extra={"event": "after_workers"})

        by_event = {record["event"]: record for record in self.capture.records}
        self.assertEqual(
            ("run-a", "alice"),
            (by_event["worker_a"]["run_id"], by_event["worker_a"]["user"]),
        )
        self.assertEqual(
            ("run-b", "bob"),
            (by_event["worker_b"]["run_id"], by_event["worker_b"]["user"]),
        )
        self.assertNotIn("run_id", by_event["after_workers"])
        self.assertNotIn("user", by_event["after_workers"])

    def test_recognized_credentials_are_redacted_recursively(self) -> None:
        get_logger("logging").warning(
            "api_key=VALUE Authorization: Bearer AUTH ?token=QUERY "
            "https://user:pass@host/path normal-tokenfan",
            extra={
                "event": "redaction_test",
                "queue_counts": {"nested": ["password=hunter2", "tokenfan"]},
            },
        )

        payload = self.capture.records[-1]
        serialized = json.dumps(payload)
        for secret in ("VALUE", "AUTH", "QUERY", "user:pass", "hunter2"):
            self.assertNotIn(secret, serialized)
        self.assertIn("[REDACTED]", payload["message"])
        self.assertIn("tokenfan", payload["message"])
        self.assertEqual("tokenfan", payload["queue_counts"]["nested"][1])

    def test_exception_omits_message_and_absolute_paths(self) -> None:
        try:
            raise RuntimeError("UNIQUE_EXCEPTION_SECRET")
        except RuntimeError:
            get_logger("sync").error(
                "Unexpected failure",
                extra={"event": "unexpected_failure"},
                exc_info=True,
            )

        payload = self.capture.records[-1]
        serialized = json.dumps(payload)
        self.assertNotIn("UNIQUE_EXCEPTION_SECRET", serialized)
        self.assertEqual("RuntimeError", payload["exception"]["type"])
        self.assertTrue(payload["exception"]["frames"])
        for frame in payload["exception"]["frames"]:
            self.assertEqual(os.path.basename(frame["file"]), frame["file"])
            self.assertFalse(os.path.isabs(frame["file"]))
            self.assertIsInstance(frame["line"], int)
            self.assertTrue(frame["function"])

    def test_setup_is_idempotent_and_invalid_level_warning_has_schema(self) -> None:
        buffer = io.StringIO()
        self.base_logger.handlers = []
        original_stdout = __import__("sys").stdout
        try:
            __import__("sys").stdout = buffer
            setup_logger("NOT_A_LEVEL")
            setup_logger("INFO")
            self.assertEqual(1, len(self.base_logger.handlers))
        finally:
            __import__("sys").stdout = original_stdout

        payload = json.loads(buffer.getvalue().splitlines()[0])
        self.assertEqual("log_level_invalid", payload["event"])
        self.assertEqual("logging", payload["component"])
        self.assertEqual("WARNING", payload["level"])


if __name__ == "__main__":
    unittest.main()
