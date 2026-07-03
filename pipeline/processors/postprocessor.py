"""
Thermal Postprocessor — interprets ONNX model output and takes action.

Interface contract (required by tedge-pipeline-runner):
    handle_output(config, model_outputs, metadata, mqtt_client) -> dict

Actions on alert:
    1. Render annotated thermal image (JPEG)
    2. Publish c8y_ThermalAlert event with annotated image attached to Cumulocity

Standardized metrics (published every cycle via the runner):
    model_score     — the key model output (max temperature in this case)
    score_threshold — the threshold being compared against
    is_alert        — binary 0/1 flag
    frame_min_temp  — raw captured frame minimum temperature
    frame_max_temp  — raw captured frame maximum temperature
    frame_mean_temp — raw captured frame mean temperature
"""

import io
import os
import json
import logging
import subprocess
import tempfile
import numpy as np
from datetime import datetime, timezone

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as patches
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

log = logging.getLogger("postprocessor")

if not HAS_MPL:
    log.warning(
        "matplotlib not available — alert images will NOT be attached to events. "
        "Fix: sudo apt-get install -y python3-matplotlib python3-pil"
    )


def handle_output(config, model_outputs, metadata, mqtt_client):
    settings = config.get("settings", {})

    # ── Parse model outputs ──
    # heat_grid holds raw average temperature per grid cell (°C) — the model
    # does no thresholding, so the alert decision lives here and reacts
    # immediately to config changes (no model rebuild/push needed).
    max_temp = float(model_outputs[0].flatten()[0])
    heat_grid = model_outputs[1][0, 0]
    threshold = settings.get("temp_threshold_celsius", 75.0)

    alert = heat_grid.max() > threshold

    # ── Standardized metrics (always published) ──
    metrics = {
        "model_score": {"value": round(max_temp, 2), "unit": "°C"},
        "score_threshold": {"value": threshold, "unit": "°C"},
        "is_alert": {"value": 1 if alert else 0, "unit": ""},
        "frame_min_temp": {"value": round(metadata.get("frame_min", 0.0), 2), "unit": "°C"},
        "frame_max_temp": {"value": round(metadata.get("frame_max", 0.0), 2), "unit": "°C"},
        "frame_mean_temp": {"value": round(metadata.get("frame_mean", 0.0), 2), "unit": "°C"},
    }

    alarm_type = settings.get("c8y_alarm_type", "c8y_ThermalAlarm")

    if not alert:
        mqtt_client.clear_alarm(alarm_type)
        return {"status": "normal", "metrics": metrics}

    # ═══════════════════════════════════════════════════════════
    # ALERT PATH
    # ═══════════════════════════════════════════════════════════
    grid_rows = settings.get("grid_rows", 6)
    grid_cols = settings.get("grid_cols", 8)
    frame_h = settings.get("frame_height", 120)
    frame_w = settings.get("frame_width", 160)
    cell_h = frame_h // grid_rows
    cell_w = frame_w // grid_cols

    cell = np.unravel_index(heat_grid.argmax(), heat_grid.shape)
    row, col = int(cell[0]), int(cell[1])

    bbox = {
        "topLeftX": col * cell_w,
        "topLeftY": row * cell_h,
        "bottomRightX": (col + 1) * cell_w,
        "bottomRightY": (row + 1) * cell_h,
    }

    frame_id = metadata.get("frame_id", "unknown")

    log.info(
        f"ALERT: {max_temp:.1f}°C (threshold {threshold}°C) "
        f"bbox=({bbox['topLeftX']},{bbox['topLeftY']})-"
        f"({bbox['bottomRightX']},{bbox['bottomRightY']})"
    )

    # ── Render the annotated image ──
    img_bytes = _render_annotated_image(metadata, bbox, max_temp, settings)

    # ── Publish Cumulocity event with image ──
    alert_event_type = settings.get("c8y_event_type", "c8y_ThermalAlert")
    alert_fragment = {
        "equipment_id": settings.get("equipment_id", ""),
        "equipment_name": settings.get("equipment_name", ""),
        "location": settings.get("location", ""),
        "camera": settings.get("camera_model", ""),
        "max_temperature_celsius": max_temp,
        "threshold_celsius": threshold,
        "frame_id": frame_id,
        "bounding_box": bbox,
    }
    _publish_event_with_image(
        event_type=alert_event_type,
        text=settings.get("c8y_event_text", "Thermal threshold exceeded"),
        fragment=alert_fragment,
        img_bytes=img_bytes,
        img_filename=f"{frame_id}_alert.jpg",
        mqtt_client=mqtt_client,
    )

    # ── Raise the Cumulocity alarm (stays active until temp drops back below threshold) ──
    mqtt_client.publish_alarm(
        alarm_type,
        severity=settings.get("c8y_alarm_severity", "major"),
        text=settings.get("c8y_event_text", "Thermal threshold exceeded"),
        extras=alert_fragment,
    )

    return {"status": "alert", "metrics": metrics}


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def _publish_event_with_image(event_type, text, fragment, img_bytes, img_filename,
                                mqtt_client):
    """
    Publish a Cumulocity event with an attached image using `tedge upload c8y`.
    Falls back to MQTT (without image) if the upload fails.
    """
    if img_bytes is None:
        log.warning("No image to attach — falling back to MQTT")
        mqtt_client.publish_event(event_type, text, fragment)
        return

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".jpg", prefix="thermal_", delete=False
        ) as tmp:
            tmp.write(img_bytes)
            tmp_path = tmp.name

        json_payload = {event_type: fragment}

        cmd = [
            "tedge", "upload", "c8y",
            "--file", tmp_path,
            "--mime-type", "image/jpeg",
            "--type", event_type,
            "--text", text,
            "--json", json.dumps(json_payload),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)

        if result.returncode == 0:
            log.info(f"Event uploaded with image ({len(img_bytes) // 1024} KB)")
        else:
            log.warning(
                f"tedge upload failed (code {result.returncode}): "
                f"{result.stderr.strip()[:200]} — falling back to MQTT"
            )
            mqtt_client.publish_event(event_type, text, fragment)

    except FileNotFoundError:
        log.warning("tedge CLI not found — falling back to MQTT")
        mqtt_client.publish_event(event_type, text, fragment)
    except subprocess.TimeoutExpired:
        log.warning("tedge upload timed out — falling back to MQTT")
        mqtt_client.publish_event(event_type, text, fragment)
    except Exception as e:
        log.error(f"Upload error: {e} — falling back to MQTT")
        mqtt_client.publish_event(event_type, text, fragment)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _render_annotated_image(metadata, bbox, max_temp, settings):
    """Render annotated thermal image as JPEG bytes (with bounding box and temperature label)."""
    if not HAS_MPL:
        return None

    temp_matrix = metadata.get("temp_matrix")
    if temp_matrix is None:
        return None

    fig, ax = plt.subplots(1, 1, figsize=(6, 4.5), dpi=80)
    # Scale off this frame's own temperature spread (1st/99th percentile, robust to a
    # few outlier hot pixels) instead of a fixed range. A hardcoded vmin here previously
    # sat above typical ambient background temperature, so imshow clipped the whole
    # background to solid black and only the already-flagged hot region showed any color.
    vmin = float(np.percentile(temp_matrix, 1))
    vmax = float(np.percentile(temp_matrix, 99))
    if vmax - vmin < 1.0:
        vmax = vmin + 1.0
    ax.imshow(temp_matrix, cmap="inferno", vmin=vmin, vmax=vmax)

    bw = bbox["bottomRightX"] - bbox["topLeftX"]
    bh = bbox["bottomRightY"] - bbox["topLeftY"]
    rect = patches.Rectangle(
        (bbox["topLeftX"], bbox["topLeftY"]), bw, bh,
        linewidth=2, edgecolor="red", facecolor="none",
    )
    ax.add_patch(rect)

    cx = (bbox["topLeftX"] + bbox["bottomRightX"]) / 2
    cy = (bbox["topLeftY"] + bbox["bottomRightY"]) / 2
    ax.plot(cx, cy, "r+", markersize=14, markeredgewidth=2)

    ax.annotate(
        f"{max_temp:.1f}°C",
        xy=(cx, bbox["topLeftY"] - 2),
        fontsize=11, color="white", fontweight="bold",
        ha="center", va="bottom",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="red", alpha=0.9),
    )

    ax.set_title(
        f"{settings.get('equipment_name', '')} - {settings.get('equipment_id', '')}",
        fontsize=9, color="gray",
    )
    ax.axis("off")
    plt.tight_layout(pad=0.5)

    buf = io.BytesIO()
    fig.savefig(buf, format="jpeg", dpi=80, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
