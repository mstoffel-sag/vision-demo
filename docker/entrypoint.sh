#!/usr/bin/env bash
#
# Container entrypoint for the vision-demo pipeline.
#
# thin-edge.io runs on the host; this container reaches it over host networking:
#   - the runner publishes measurements/events/alarms over MQTT (localhost:1883,
#     set via mqtt_host/mqtt_port in pipeline.json)
#   - the postprocessor uploads alert images by calling thin-edge's local
#     Cumulocity HTTP proxy (localhost:8001) directly — no tedge CLI, no device
#     certificate needed in the container (the proxy injects auth on the host).
set -euo pipefail

# otc_capture writes into this dir but does not create it (see vision-demo README).
mkdir -p /opt/tedge-pipeline/data/captures

echo "[entrypoint] Starting pipeline runner..."
exec python3 /opt/tedge-pipeline/pipeline_runner.py \
    --config /opt/tedge-pipeline/config/pipeline.json "$@"
