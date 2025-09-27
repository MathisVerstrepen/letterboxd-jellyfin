#!/bin/bash
set -e

# Load sync_interval from config.yaml, default to 10 minutes if not found
SYNC_INTERVAL_MINUTES=$(python3 -c "import yaml; f=open('config.yaml'); d=yaml.safe_load(f); print(d.get('system', {}).get('sync_interval', 10)); f.close()")
SYNC_INTERVAL_SECONDS=$((SYNC_INTERVAL_MINUTES * 60))

echo "--- Letterboxd-Jellyfin Sync Service ---"
echo "Starting sync loop. Interval is set to ${SYNC_INTERVAL_MINUTES} minutes."

# Run once immediately on startup
echo "[$(date)] --- Performing initial sync run... ---"
python3 main.py

# Loop indefinitely
while true; do
  echo "[$(date)] --- Sync finished. Next run in ${SYNC_INTERVAL_MINUTES} minutes. ---"
  sleep ${SYNC_INTERVAL_SECONDS}
  echo "[$(date)] --- Starting scheduled sync run... ---"
  python3 main.py
done
