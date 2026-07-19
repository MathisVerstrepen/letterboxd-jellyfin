import errno
import signal
import threading
import time
from datetime import UTC, datetime
from typing import Any

from src.config import load_config
from src.exceptions import ConfigurationError
from src.jellyfin import Jellyfin
from src.logger import get_logger, log_context, new_run_id, setup_logger
from src.observability import ObservabilityService
from src.radarr import RadarrClient
from src.results import empty_failures, empty_queue_counts
from src.state_manager import load_state, save_state
from src.sync import SyncManager


def utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def run_sync_cycle(
    config: dict[str, Any],
    observability: ObservabilityService,
    run_sequence: int,
) -> str:
    with log_context(run_id=new_run_id()):
        return _run_sync_cycle(config, observability, run_sequence)


def _run_sync_cycle(
    config: dict[str, Any],
    observability: ObservabilityService,
    run_sequence: int,
) -> str:
    logger = get_logger("scheduler")
    failures = empty_failures()
    queue_counts = empty_queue_counts()
    completed_users = 0
    state_persistence_ok = True
    outer_cycle_failed = False
    started_datetime = datetime.now(UTC)
    started_at = utc_timestamp(started_datetime)
    started_monotonic = time.monotonic()

    observability.begin_cycle()
    try:
        logger.info(
            "Sync run started",
            extra={"event": "sync_run_started"},
        )
        state_result = load_state()
        sync_state = state_result.data
        failures["state"] += state_result.failed_items
        if state_result.failed_items:
            state_persistence_ok = False

        clients_ready = False
        try:
            jellyfin_config = config["jellyfin"]
            radarr_config = config["radarr"]
            jellyfin_client = Jellyfin(
                url=jellyfin_config["url"], api_key=jellyfin_config["api_key"]
            )
            radarr_client = RadarrClient(
                url=radarr_config["url"],
                api_key=radarr_config["api_key"],
                timeout=radarr_config.get("timeout", 60),
            )
            clients_ready = True
        except Exception:
            failures["runtime"] += 1
            logger.error(
                "Sync clients could not be initialized",
                extra={"event": "sync_client_initialization_failed", "stage": "runtime"},
                exc_info=True,
            )

        if clients_ready:
            for user_config in config.get("users", []):
                if not isinstance(user_config, dict):
                    failures["configuration"] += 1
                    logger.warning(
                        "Skipping invalid user configuration",
                        extra={"event": "sync_user_failed", "stage": "configuration"},
                    )
                    continue
                username = user_config.get("letterboxd_username")
                jellyfin_username = user_config.get("jellyfin_username")
                if not username or not jellyfin_username:
                    failures["configuration"] += 1
                    logger.warning(
                        "Skipping user with missing required configuration",
                        extra={"event": "sync_user_failed", "stage": "configuration"},
                    )
                    continue

                with log_context(user=username):
                    try:
                        manager = SyncManager(
                            user_config,
                            jellyfin_client,
                            radarr_client,
                            sync_state.get(username),
                            config.get("letterboxd", {}),
                            radarr_config,
                        )
                        result = manager.run()
                    except Exception:
                        failures["runtime"] += 1
                        logger.error(
                            "Unexpected user sync failure",
                            extra={"event": "sync_user_failed", "stage": "runtime"},
                            exc_info=True,
                        )
                        continue
                for stage, count in result.failures_by_stage.items():
                    failures[stage] += count
                for queue, count in result.queue_counts.items():
                    queue_counts[queue] += count
                if result.completed:
                    completed_users += 1
                if result.state_advance_id:
                    sync_state[username] = result.state_advance_id

            save_result = save_state(sync_state)
            failures["state"] += save_result.failed_items
            if save_result.failed_items:
                state_persistence_ok = False
    except Exception:
        outer_cycle_failed = True
        failures["runtime"] += 1
        logger.error(
            "Unexpected sync cycle failure",
            extra={"event": "sync_run_failed", "stage": "runtime"},
            exc_info=True,
        )
    finally:
        finished_datetime = datetime.now(UTC)
        finished_at = utc_timestamp(finished_datetime)
        duration = round(time.monotonic() - started_monotonic, 3)
        failed_items = sum(failures.values())
        if completed_users == 0 or not state_persistence_ok or outer_cycle_failed:
            outcome = "failed"
        elif failed_items:
            outcome = "partial"
        else:
            outcome = "success"
        try:
            observability.complete_cycle(
                run_id=run_sequence,
                outcome=outcome,
                started_at=started_at,
                finished_at=finished_at,
                finished_timestamp=finished_datetime.timestamp(),
                duration_seconds=duration,
                failures_by_stage=failures,
                queue_counts=queue_counts,
            )
        finally:
            observability.end_cycle()
        logger.info(
            "Sync run completed",
            extra={
                "event": "sync_run_completed",
                "outcome": outcome,
                "duration_seconds": duration,
                "failed_items": failed_items,
                "queue_counts": queue_counts,
            },
        )
    return outcome


def main() -> int:
    setup_logger("INFO")
    logger = get_logger("service")
    try:
        config = load_config()
    except ConfigurationError:
        logger.error(
            "Configuration is missing or invalid",
            extra={"event": "configuration_invalid", "stage": "configuration"},
            exc_info=True,
        )
        return 1

    setup_logger(config.get("system", {}).get("log_level", "INFO"))
    host = config.get("observability", {}).get("host", "127.0.0.1")
    port = config.get("observability", {}).get("port", 8000)
    interval_seconds = config.get("system", {}).get("sync_interval", 10) * 60
    stop_event = threading.Event()
    observability = ObservabilityService(host, port)

    def request_stop(signum: int, frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logger.info("Service starting", extra={"event": "service_starting"})

    try:
        try:
            observability.start()
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                logger.error(
                    "Configured observability port is already in use; free the port "
                    "or change observability.port in config.yaml",
                    extra={
                        "event": "configured_observability_port_already_in_use",
                        "host": host,
                        "port": port,
                    },
                    exc_info=True,
                )
            else:
                logger.error(
                    "Observability server could not start",
                    extra={
                        "event": "observability_server_start_failed",
                        "host": host,
                        "port": port,
                    },
                    exc_info=True,
                )
            return 1
        logger.info(
            "Observability server started",
            extra={"event": "observability_server_started", "host": host, "port": port},
        )

        run_sequence = 0
        while not stop_event.is_set():
            run_sequence += 1
            run_sync_cycle(config, observability, run_sequence)
            if stop_event.is_set():
                break
            observability.set_scheduler_state("sleeping")
            stop_event.wait(interval_seconds)
        return 0
    except Exception:
        get_logger("scheduler").error(
            "Unexpected scheduler failure",
            extra={"event": "scheduler_failed", "stage": "runtime"},
            exc_info=True,
        )
        return 1
    finally:
        logger.info("Service stopping", extra={"event": "service_stopping"})
        observability.stop()
        logger.info(
            "Observability server stopped",
            extra={"event": "observability_server_stopped"},
        )


if __name__ == "__main__":
    raise SystemExit(main())
