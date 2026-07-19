# Health and observability

The embedded listener exposes three fixed, unauthenticated `GET` routes:

- `GET /health` is process liveness. It returns HTTP 200 while the scheduler is running or sleeping, regardless of external service failures, and 503 during orderly shutdown. Docker probes this route, so a Radarr, Jellyfin, or Letterboxd outage does not create a container restart loop.
- `GET /ready` returns 503 until a cycle succeeds. It returns 200 only when the most recently completed cycle succeeded; a later partial or failed cycle changes it back to 503. A cycle currently in progress retains the previous completed result.
- `GET /metrics` returns Prometheus exposition data.

`/health` and `/ready` return the same aggregate JSON document. It includes liveness, readiness, scheduler state, the latest successful completion timestamp, and the last cycle's outcome, duration, failed-item counts by fixed stage, and queue counts. It does not include movie titles/IDs, usernames, URLs, credentials, or error text. Before the first completed cycle, `last_run` and `last_successful_run` are `null`. Status and counters are process-local and reset after restart.

The default `127.0.0.1:8000` bind is intentionally host-local. Production Compose uses host networking, so query it on the Docker host, for example `http://127.0.0.1:8000/health`. To permit a remote Prometheus server to connect, set `observability.host` to `0.0.0.0` and secure access at the host/network boundary; these endpoints do not provide authentication or TLS. No Compose port mapping is needed with host networking.

Prometheus exports these fixed-cardinality series:

| Metric | Type and fixed labels | Meaning and initial value |
|---|---|---|
| `letterboxd_jellyfin_sync_runs_total` | Counter; `outcome` is exactly `success`, `partial`, or `failed` | Completed cycles since process start. Every outcome series exists at `0` before the first cycle completes. |
| `letterboxd_jellyfin_sync_run_duration_seconds` | Histogram without labels; buckets are `1`, `5`, `15`, `30`, `60`, `120`, `300`, `600`, `1800`, `3600`, and `+Inf` seconds | Distribution of completed cycle durations; its count and sum begin at `0`. |
| `letterboxd_jellyfin_sync_last_run_timestamp_seconds` | Gauge without labels | Unix completion time of the latest cycle; `0` before a cycle completes. |
| `letterboxd_jellyfin_sync_last_success_timestamp_seconds` | Gauge without labels | Unix completion time of the latest successful cycle; `0` until the first success. |
| `letterboxd_jellyfin_sync_last_run_duration_seconds` | Gauge without labels | Duration of the latest completed cycle in seconds; initially `0`. |
| `letterboxd_jellyfin_sync_last_run_failed_items` | Gauge without labels | Failed work units in the latest completed cycle; initially `0`. |
| `letterboxd_jellyfin_sync_failed_items_total` | Counter; `stage` is exactly `configuration`, `letterboxd`, `radarr`, `jellyfin`, `state`, or `runtime` | Failed work units since process start. Every stage series exists at `0` initially. |
| `letterboxd_jellyfin_sync_last_run_queue_items` | Gauge; `queue` is exactly `radarr_add`, `jellyfin_add`, or `jellyfin_remove` | Local work admitted by the latest completed cycle for that queue; every queue series is initially `0`. |
| `letterboxd_jellyfin_sync_in_progress` | Gauge without labels | `1` while a sync cycle is running and `0` otherwise; initially `0`. |
| `letterboxd_jellyfin_ready` | Gauge without labels | The same readiness boolean used by `/ready`: `1` only when the latest completed cycle succeeded, otherwise `0`; initially `0`. |

The `radarr_add`, `jellyfin_add`, and `jellyfin_remove` queue values count local work admitted by the latest cycle. They are not retry counts, successful-operation counts, or Radarr's remote queue depth.

## Grafana dashboard template

[`docs/grafana/letterboxd-jellyfin-overview.json`](grafana/letterboxd-jellyfin-overview.json) is an importable Grafana dashboard for the metrics above. It includes service and readiness status, scheduler state, sync freshness and duration, outcomes, queue activity, and failures by stage. Select a Prometheus data source during import. The dashboard expects Prometheus to scrape the application with `job_name: letterboxd-jellyfin`.

Runtime stdout is one JSON object per log record. Logs include a stable `event` and safe aggregate context, making them suitable for Docker log collection without emitting API keys or proxy credentials.
