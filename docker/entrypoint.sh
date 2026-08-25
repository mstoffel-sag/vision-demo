#!/usr/bin/env bash
#
# Container entrypoint for the vision-demo pipeline.
#
# Runs two things side by side:
#
#   1. A long-lived `otc_capture` process that holds ONE connection to the
#      camera open and writes a frame every `capture_interval_ms`. Keeping the
#      connection open is the point: a fresh connect per frame makes the SDK run
#      a startup NUC every time (the shutter flag audibly closes, ~20s before
#      valid data). Held open, the flag only closes on the camera's own periodic
#      NUC schedule.
#
#   2. The onnx-pipeline-runner loop, whose preprocessor now just reads the
#      newest frame off disk instead of shelling out to otc_capture.
#
#      The camera admits a single client, so exactly one process may connect —
#      that is the capture loop above. The preprocessor only reads files.
#
# thin-edge.io runs on the host; this container reaches it over host networking:
#   - the runner publishes measurements/events/alarms over MQTT (localhost:1883,
#     set via mqtt_host/mqtt_port in pipeline.json)
#   - the postprocessor uploads alert images by calling thin-edge's local
#     Cumulocity HTTP proxy (localhost:8001) directly — no tedge CLI, no device
#     certificate needed in the container (the proxy injects auth on the host).
set -euo pipefail

CONFIG=/opt/tedge-pipeline/config/pipeline.json
CAPTURE_DIR=/opt/tedge-pipeline/data/captures

# otc_capture writes into this dir but does not create it (see vision-demo README).
mkdir -p "$CAPTURE_DIR"

# Camera settings are read ONCE here, at container start. pipeline.json is still
# hot-reloaded by the runner for everything else (thresholds, device metadata,
# frame staleness), but changing a camera_* / capture_* value now needs a
# container restart, because it reconfigures this long-lived capture process.
eval "$(python3 - "$CONFIG" <<'PY'
import json, shlex, sys

settings = json.load(open(sys.argv[1])).get("settings", {})

def emit(name, value):
    print(f"{name}={shlex.quote(str(value))}")

emit("CAPTURE_BIN",     settings.get("capture_binary", "otc_capture"))
emit("CAM_SERIAL",      settings.get("camera_serial", 0))
emit("CAM_IP",          str(settings.get("camera_ip", "")).strip())
emit("CAM_PORT",        settings.get("camera_port", 50101))
emit("CAP_NETWORK",     settings.get("capture_network", "192.168.0.0/24"))
emit("CAP_TIMEOUT",     settings.get("capture_timeout_s", 30))
emit("CAP_INTERVAL_MS", settings.get("capture_interval_ms", 5000))
PY
)"

# --count has no "run forever" value, so use one large enough to outlive any
# deployment: at the 5s default this is ~150 years. Changing otc_capture.cpp
# would mean rebuilding the image; this keeps the fix to bind-mounted files.
CAPTURE_ARGS=(
    --outdir      "$CAPTURE_DIR"
    --serial      "$CAM_SERIAL"
    --timeout-s   "$CAP_TIMEOUT"
    --count       1000000000
    --interval-ms "$CAP_INTERVAL_MS"
    --csv
)
if [ -n "$CAM_IP" ]; then
    CAPTURE_ARGS+=(--ip "$CAM_IP" --port "$CAM_PORT")
else
    CAPTURE_ARGS+=(--network "$CAP_NETWORK")
fi

# Supervise the capture process: if the camera drops or the SDK gives up, the
# loop reconnects. Without this a single failure would stop frames forever —
# the per-cycle spawn it replaces was self-healing by construction.
capture_supervisor() {
    local child=
    trap 'kill "$child" 2>/dev/null || true; exit 0' TERM INT
    while true; do
        echo "[capture] $CAPTURE_BIN ${CAPTURE_ARGS[*]}"
        "$CAPTURE_BIN" "${CAPTURE_ARGS[@]}" &
        child=$!
        wait "$child" || echo "[capture] otc_capture exited (rc=$?) — restarting in 5s"
        sleep 5
    done
}

capture_supervisor &
CAPTURE_PID=$!

echo "[entrypoint] Starting pipeline runner..."
python3 /opt/tedge-pipeline/pipeline_runner.py --config "$CONFIG" "$@" &
RUNNER_PID=$!

# Stop the capture process on shutdown so the camera's claim is released. A
# killed-mid-connection client leaves a stuck claim that only a power-cycle
# clears (see the README troubleshooting note on "busy with another client").
shutdown() { kill "$CAPTURE_PID" "$RUNNER_PID" 2>/dev/null || true; }
trap shutdown TERM INT EXIT

# The supervisor never returns, so this waits on the runner.
wait -n "$CAPTURE_PID" "$RUNNER_PID"
