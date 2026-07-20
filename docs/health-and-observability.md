# Health and observability

Letterboxd-Jellyfin runs an embedded operational HTTP server alongside the sync
scheduler. Prometheus uses the pull model and periodically requests `/metrics`; the
application does not push metrics or require a separate exporter.

## HTTP endpoints

The listener exposes three fixed, unauthenticated `GET` routes:

- `GET /health` reports process liveness. It returns HTTP 200 while the scheduler is running or sleeping, regardless of external service failures, and 503 during orderly shutdown. Docker probes this route, so a Radarr, Sonarr, Jellyfin, or Letterboxd outage does not create a container restart loop.
- `GET /ready` reports the latest completed sync result. It returns 503 until a cycle succeeds and returns 200 only when the most recently completed cycle fully succeeded. A later partial or failed cycle changes it back to 503. A cycle in progress retains the previous completed result.
- `GET /metrics` returns Prometheus exposition data.

`/health` and `/ready` return the same aggregate JSON document. It contains
liveness, readiness, scheduler state, the latest successful completion timestamp,
and the last cycle's outcome, duration, failed-item counts, and queue counts. It
does not contain movie titles or IDs, usernames, URLs, credentials, or error text.
Before the first completed cycle, `last_run` and `last_successful_run` are `null`.

## Listener configuration

Configure the listener in `config.yaml`:

```yaml
observability:
  host: "127.0.0.1"
  port: 8000
```

The default loopback bind is appropriate when Prometheus runs directly on the same
host. Production Compose uses host networking, so no Compose port mapping is
required. The endpoints are then available on the Docker host at, for example,
`http://127.0.0.1:8000/health`.

A Prometheus container using a bridge network cannot reach a service bound only to
the host's loopback interface. For that deployment, listen on all host interfaces:

```yaml
observability:
  host: "0.0.0.0"
  port: 8000
```

Restart Letterboxd-Jellyfin after changing the listener configuration. Because the
endpoints do not provide authentication or TLS, restrict the port with host firewall
or network policy when binding to `0.0.0.0`.

## Prometheus integration

The Grafana template and example alert rules expect the exact Prometheus job name
`letterboxd-jellyfin`.

### Prometheus on the same host

When Prometheus runs directly on the Docker host and the application uses the
default listener, add this job to `scrape_configs`:

```yaml
scrape_configs:
  - job_name: letterboxd-jellyfin
    scrape_interval: 15s
    static_configs:
      - targets:
          - 127.0.0.1:8000
```

### Prometheus in Docker

When Prometheus runs in a bridge-networked container, make the Docker host resolvable
on Linux:

```yaml
services:
  prometheus:
    extra_hosts:
      - host.docker.internal:host-gateway
```

Set `observability.host` to `0.0.0.0`, then use the configured listener port in the
Prometheus target:

```yaml
scrape_configs:
  - job_name: letterboxd-jellyfin
    scrape_interval: 15s
    static_configs:
      - targets:
          - host.docker.internal:8000
```

If Prometheus and Letterboxd-Jellyfin share a user-defined Docker network instead,
use the application service name as the target, such as
`letterboxd-sync:8000`. This requires removing host networking from the application
and publishing or exposing any host services it needs through that network.

### Validate the integration

Check the endpoint before reloading Prometheus:

```bash
curl --fail http://127.0.0.1:8000/metrics
```

Validate a containerized Prometheus configuration and reload or recreate the service:

```bash
docker exec prometheus promtool check config /etc/prometheus/prometheus.yml
docker compose up -d prometheus
```

In Prometheus, open **Status > Target health** and confirm that the
`letterboxd-jellyfin` target is `UP`. The equivalent PromQL check is:

```promql
up{job="letterboxd-jellyfin"}
```

The result should be `1`. A result of `0` means Prometheus knows about the target but
cannot scrape it. No result means the scrape job has not been loaded or the job name
does not match.

## Metrics reference

Prometheus exports these fixed-cardinality series:

| Metric | Type and fixed labels | Meaning and initial value |
|---|---|---|
| `letterboxd_jellyfin_sync_runs_total` | Counter; `outcome` is exactly `success`, `partial`, or `failed` | Completed cycles since process start. Every outcome series exists at `0` before the first cycle completes. |
| `letterboxd_jellyfin_sync_run_duration_seconds` | Histogram without labels; buckets are `1`, `5`, `15`, `30`, `60`, `120`, `300`, `600`, `1800`, `3600`, and `+Inf` seconds | Distribution of completed cycle durations; its count and sum begin at `0`. |
| `letterboxd_jellyfin_sync_last_run_timestamp_seconds` | Gauge without labels | Unix completion time of the latest cycle; `0` before a cycle completes. |
| `letterboxd_jellyfin_sync_last_success_timestamp_seconds` | Gauge without labels | Unix completion time of the latest successful cycle; `0` until the first success. |
| `letterboxd_jellyfin_sync_last_run_duration_seconds` | Gauge without labels | Duration of the latest completed cycle in seconds; initially `0`. |
| `letterboxd_jellyfin_sync_last_run_failed_items` | Gauge without labels | Failed work units in the latest completed cycle; initially `0`. |
| `letterboxd_jellyfin_sync_failed_items_total` | Counter; `stage` is exactly `configuration`, `letterboxd`, `radarr`, `sonarr`, `jellyfin`, `state`, or `runtime` | Failed work units since process start. Every stage series exists at `0` initially. |
| `letterboxd_jellyfin_sync_last_run_queue_items` | Gauge; `queue` is exactly `radarr_add`, `sonarr_add`, `jellyfin_add`, or `jellyfin_remove` | Local work attempted or admitted by the latest completed cycle for that queue; every queue series is initially `0`. |
| `letterboxd_jellyfin_sync_in_progress` | Gauge without labels | `1` while a sync cycle is running and `0` otherwise; initially `0`. |
| `letterboxd_jellyfin_ready` | Gauge without labels | The same readiness boolean used by `/ready`: `1` only when the latest completed cycle succeeded, otherwise `0`; initially `0`. |

The queue values count local work in the latest cycle, not remote queue depth or successful-operation counts. In particular, `sonarr_add` counts actual Sonarr series POST attempts, including retried requests; it does not represent Sonarr's missing-episode queue depth.

Metrics are process-local. Counters and latest-run gauges reset when the application
restarts. Prometheus functions such as `increase()` account for counter resets, but
the latest-run values remain at zero until the restarted process completes a cycle.

## Alert rules

The following rules cover endpoint availability, unsuccessful cycles, and stale
successful syncs:

```yaml
groups:
  - name: letterboxd-jellyfin
    rules:
      - alert: LetterboxdJellyfinDown
        expr: up{job="letterboxd-jellyfin"} == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "Letterboxd-Jellyfin metrics endpoint is down"
          description: "Prometheus has been unable to scrape the sync service for more than 2 minutes."

      - alert: LetterboxdJellyfinSyncFailed
        expr: letterboxd_jellyfin_ready{job="letterboxd-jellyfin"} == 0 and letterboxd_jellyfin_sync_last_run_timestamp_seconds{job="letterboxd-jellyfin"} > 0
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "The latest Letterboxd-Jellyfin sync did not fully succeed"
          description: "Inspect the failure-stage metrics and application logs for the latest sync cycle."

      - alert: LetterboxdJellyfinSyncStale
        expr: letterboxd_jellyfin_sync_last_success_timestamp_seconds{job="letterboxd-jellyfin"} > 0 and time() - letterboxd_jellyfin_sync_last_success_timestamp_seconds{job="letterboxd-jellyfin"} > 2 * 60 * 60
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Letterboxd-Jellyfin has not completed a successful sync recently"
          description: "The last successful sync completed more than 2 hours ago."
```

Choose the stale threshold based on `system.sync_interval` and normal run duration.
It should allow at least two expected cycles plus enough time for the longest normal
sync. The two-hour example is suitable for a 30-minute interval.

## Grafana dashboard template

[`docs/grafana/letterboxd-jellyfin-overview.json`](grafana/letterboxd-jellyfin-overview.json)
is an importable dashboard for the metrics above. It includes endpoint and readiness
status, scheduler state, sync freshness and duration, outcomes, queue activity, and
failures by stage.

For a manual import, open **Dashboards > New > Import**, select the JSON file, and
choose the Prometheus data source. The dashboard uses a data-source variable and
expects `job_name: letterboxd-jellyfin`.

For file provisioning, mount the template directory into Grafana:

```yaml
services:
  grafana:
    volumes:
      - ../letterboxd-jellyfin/docs/grafana:/var/lib/grafana/letterboxd-jellyfin-dashboards:ro
```

Then add a dashboard provider:

```yaml
apiVersion: 1

providers:
  - name: Letterboxd-Jellyfin
    orgId: 1
    folder: Applications
    type: file
    disableDeletion: false
    allowUiUpdates: true
    updateIntervalSeconds: 30
    options:
      path: /var/lib/grafana/letterboxd-jellyfin-dashboards
```

Restart or recreate Grafana after adding a new bind mount. Subsequent JSON changes
are detected at the provider's update interval.

## Structured logs

Runtime stdout contains one compact JSON object per log record. Every record has the following stable fields:

| Field | Presence and type | Meaning |
| --- | --- | --- |
| `timestamp` | Required string | UTC ISO-8601 timestamp with milliseconds and a trailing `Z`. |
| `level` | Required string | Python log level name. |
| `logger` | Required string | Logger name, normally `letterboxd-sync`. |
| `event` | Required string | Stable snake-case event identifier; unclassified records use `runtime_log`. |
| `message` | Required string | Fixed human-readable description. |
| `component` | Required string | Current owner: `logging`, `service`, `scheduler`, `sync`, `state`, `letterboxd`, `proxy`, `radarr`, `sonarr`, `jellyfin`, or `observability`. |
| `run_id` | Optional UUID4 string | Log-only correlation ID shared by one sync cycle. |
| `user` | Optional string | Letterboxd username for user-scoped work. |

Event-specific fields are allowlisted. Current operational fields are `stage`, `outcome`, `duration_seconds`, `failed_items`, `queue_counts`, `queue`, `count`, `attempt`, `status_code`, `path`, `host`, `port`, `attempted`, `succeeded`, and `skipped_items`. Unknown extras are omitted. Event names generally follow `<component>_<operation>_<state>`. DEBUG is diagnostic request or batch detail, INFO is lifecycle or successful aggregate completion, WARNING is retryable or partial degradation, and ERROR is an exhausted, rejected, invalid, or unexpected operation.

The UUID log `run_id` is separate from the scheduler's integer cycle sequence. The health and readiness payloads retain that integer as `last_run.run_id`; their contract has not changed. User and run context is inherited by provider work, including Letterboxd detail workers, and is removed when its scope ends.

Successful Radarr queue items are summarized once per user by `radarr_queue_completed` with `attempted`, `succeeded`, `failed_items`, and `outcome`. Letterboxd TV skips are summarized by `letterboxd_scrape_completed` with `count`, `skipped_items`, `failed_items`, and `outcome`. During migration, consumers must also replace the former log-only `letterboxd_username` field with `user`; the per-item `radarr_queue_result` success records and `letterboxd_tv_skipped` records are no longer emitted. Retry and terminal failure events remain available.

Logs must not contain API keys, authorization or cookie values, proxy credentials, full provider or proxy URLs, request or response bodies and headers, Jellyfin usernames, provider/internal IDs, collection IDs, TMDB IDs, titles, or exception messages. The Letterboxd username is the only approved user identity. Fixed messages and the structured-field allowlist are the primary controls. As defense in depth, recognized credential assignments and query parameters (`api_key`/`apikey`, `authorization`, `cookie`, `password`, `token`, and `secret`) and URL user information are replaced with `[REDACTED]`; exception metadata includes only the exception type and traceback file basename, function, and line. Arbitrary opaque secrets cannot be reliably detected, so runtime values must not be placed in messages or unapproved fields.

The production application container uses Docker's `json-file` driver with three `10m` segments, bounding local log retention to roughly 30 MB. This repository does not deploy a collector. External collectors may consume container stdout through their supported Docker integration and should parse each line as one JSON object.

## Troubleshooting

If the Prometheus target is down, check these in order:

1. Request `/metrics` from the Docker host using the configured port.
2. Confirm `observability.host` permits connections from Prometheus's network.
3. Confirm `host.docker.internal` resolves inside the Prometheus container when using the Docker-host target.
4. Confirm the target port matches `observability.port`.
5. Confirm Prometheus loaded the job and that its name is exactly `letterboxd-jellyfin`.
6. Check the Letterboxd-Jellyfin logs for `observability_server_start_failed`, which usually indicates a bind-address or port conflict.

If Grafana panels show no data while the Prometheus target is up, verify the selected
Prometheus data source and evaluate `up{job="letterboxd-jellyfin"}` directly. Panels
for the last run or last success intentionally have no meaningful value until a cycle
has completed after process startup.
