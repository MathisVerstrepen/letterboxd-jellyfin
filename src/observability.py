import copy
import json
import logging
import socket
import threading
from http.server import ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.exposition import MetricsHandler

from src.results import FAILURE_STAGES, QUEUE_NAMES


class _OperationalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        logging.getLogger("letterboxd-sync").error(
            "Operational HTTP request failed",
            extra={"event": "observability_request_failed"},
            exc_info=True,
        )


class ObservabilityService:
    """Own the operational HTTP server, status snapshot, and metrics registry."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.logger = logging.getLogger("letterboxd-sync")
        self._lock = threading.RLock()
        self._snapshot: dict[str, Any] = {
            "service": "letterboxd-jellyfin",
            "live": True,
            "ready": False,
            "scheduler_state": "starting",
            "last_successful_run": None,
            "last_run": None,
        }
        self.registry = CollectorRegistry()
        self._runs = Counter(
            "letterboxd_jellyfin_sync_runs_total",
            "Completed sync cycles since process start.",
            ("outcome",),
            registry=self.registry,
        )
        self._duration = Histogram(
            "letterboxd_jellyfin_sync_run_duration_seconds",
            "Distribution of completed sync cycle durations.",
            buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600),
            registry=self.registry,
        )
        self._last_timestamp = Gauge(
            "letterboxd_jellyfin_sync_last_run_timestamp_seconds",
            "Unix completion time of the latest sync cycle.",
            registry=self.registry,
        )
        self._last_success_timestamp = Gauge(
            "letterboxd_jellyfin_sync_last_success_timestamp_seconds",
            "Unix completion time of the latest successful sync cycle.",
            registry=self.registry,
        )
        self._last_duration = Gauge(
            "letterboxd_jellyfin_sync_last_run_duration_seconds",
            "Duration of the latest completed sync cycle.",
            registry=self.registry,
        )
        self._last_failed = Gauge(
            "letterboxd_jellyfin_sync_last_run_failed_items",
            "Failed work units in the latest completed sync cycle.",
            registry=self.registry,
        )
        self._failed = Counter(
            "letterboxd_jellyfin_sync_failed_items_total",
            "Failed work units since process start by fixed stage.",
            ("stage",),
            registry=self.registry,
        )
        self._last_queue = Gauge(
            "letterboxd_jellyfin_sync_last_run_queue_items",
            "Local work items admitted by the latest cycle, not remote queue depth.",
            ("queue",),
            registry=self.registry,
        )
        self._in_progress = Gauge(
            "letterboxd_jellyfin_sync_in_progress",
            "Whether a sync cycle is currently in progress.",
            registry=self.registry,
        )
        self._ready = Gauge(
            "letterboxd_jellyfin_ready",
            "Whether the latest completed sync cycle was successful.",
            registry=self.registry,
        )
        for outcome in ("success", "partial", "failed"):
            self._runs.labels(outcome=outcome)
        for stage in FAILURE_STAGES:
            self._failed.labels(stage=stage)
        for queue in QUEUE_NAMES:
            self._last_queue.labels(queue=queue).set(0)

        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler_class = self._make_handler()
        server_class = _OperationalHTTPServer
        if ":" in self.host:
            class IPv6OperationalHTTPServer(_OperationalHTTPServer):
                address_family = socket.AF_INET6

            server_class = IPv6OperationalHTTPServer
        self._server = server_class((self.host, self.port), handler_class)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="observability-http",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self.set_scheduler_state("stopping")
        server = self._server
        thread = self._thread
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join()
        self._server = None
        self._thread = None

    def set_scheduler_state(self, state: str) -> None:
        with self._lock:
            self._snapshot["scheduler_state"] = state
            self._snapshot["live"] = state != "stopping"

    def begin_cycle(self) -> None:
        self.set_scheduler_state("running")
        self._in_progress.set(1)

    def end_cycle(self) -> None:
        self._in_progress.set(0)

    def complete_cycle(
        self,
        *,
        run_id: int,
        outcome: str,
        started_at: str,
        finished_at: str,
        finished_timestamp: float,
        duration_seconds: float,
        failures_by_stage: dict[str, int],
        queue_counts: dict[str, int],
    ) -> None:
        normalized_failures = {
            stage: int(failures_by_stage.get(stage, 0)) for stage in FAILURE_STAGES
        }
        normalized_queues = {
            queue: int(queue_counts.get(queue, 0)) for queue in QUEUE_NAMES
        }
        failed_items = sum(normalized_failures.values())
        ready = outcome == "success"

        self._runs.labels(outcome=outcome).inc()
        self._duration.observe(duration_seconds)
        self._last_timestamp.set(finished_timestamp)
        self._last_duration.set(duration_seconds)
        self._last_failed.set(failed_items)
        for stage, count in normalized_failures.items():
            self._failed.labels(stage=stage).inc(count)
        for queue, count in normalized_queues.items():
            self._last_queue.labels(queue=queue).set(count)
        self._ready.set(1 if ready else 0)
        if ready:
            self._last_success_timestamp.set(finished_timestamp)

        last_run = {
            "run_id": run_id,
            "outcome": outcome,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": duration_seconds,
            "failed_items": failed_items,
            "failures_by_stage": normalized_failures,
            "queue_counts": normalized_queues,
        }
        with self._lock:
            self._snapshot["ready"] = ready
            self._snapshot["last_run"] = last_run
            if ready:
                self._snapshot["last_successful_run"] = finished_at

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._snapshot)

    def _make_handler(self):
        service = self
        metrics_base = MetricsHandler.factory(self.registry)

        class OperationalHandler(metrics_base):
            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                if path == "/metrics":
                    service.logger.debug(
                        "Operational HTTP request",
                        extra={"event": "observability_request", "path": path, "status_code": 200},
                    )
                    super().do_GET()
                    return
                if path in ("/health", "/ready"):
                    snapshot = service.snapshot()
                    if path == "/health":
                        status = 200 if snapshot["live"] else 503
                    else:
                        status = 200 if snapshot["ready"] else 503
                    self._send_json(status, snapshot)
                    return
                self._send_json(404, {"error": "not_found"})

            def __getattr__(self, name: str) -> Any:
                if name.startswith("do_"):
                    return self._method_not_allowed
                raise AttributeError(name)

            def _method_not_allowed(self) -> None:
                self._send_json(405, {"error": "method_not_allowed"}, allow_get=True)

            def _send_json(
                self, status: int, payload: dict[str, Any], allow_get: bool = False
            ) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                if allow_get:
                    self.send_header("Allow", "GET")
                self.end_headers()
                self.wfile.write(body)
                service.logger.debug(
                    "Operational HTTP request",
                    extra={
                        "event": "observability_request",
                        "path": urlsplit(self.path).path,
                        "status_code": status,
                    },
                )

            def log_message(self, format: str, *args: Any) -> None:
                return

        return OperationalHandler
