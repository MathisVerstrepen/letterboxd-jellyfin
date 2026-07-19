from prometheus_client import generate_latest

from src.observability import ObservabilityService
from src.results import empty_failures, empty_queue_counts


def test_sonarr_fixed_dimensions_are_preinitialized_and_snapshotted():
    service = ObservabilityService("127.0.0.1", 0)
    failures = empty_failures()
    queues = empty_queue_counts()
    failures["sonarr"] = 2
    queues["sonarr_add"] = 3
    service.complete_cycle(
        run_id=1,
        outcome="partial",
        started_at="2026-01-01T00:00:00.000Z",
        finished_at="2026-01-01T00:00:01.000Z",
        finished_timestamp=1.0,
        duration_seconds=1.0,
        failures_by_stage=failures,
        queue_counts=queues,
    )
    snapshot = service.snapshot()["last_run"]
    assert snapshot["failures_by_stage"]["sonarr"] == 2
    assert snapshot["queue_counts"]["sonarr_add"] == 3
    metrics = generate_latest(service.registry).decode()
    assert 'stage="sonarr"' in metrics
    assert 'queue="sonarr_add"' in metrics
