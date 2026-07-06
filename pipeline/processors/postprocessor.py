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
import json
import uuid
import logging
import urllib.request
import urllib.error
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
        settings=settings,
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

# Resolved once and cached — the device's Cumulocity managed-object id, needed
# as the `source` when creating events over the REST proxy.
_source_id_cache = None


def _proxy_base(settings):
    """Base URL of thin-edge's local Cumulocity HTTP proxy (auth is injected by
    thin-edge, so no credentials are needed here)."""
    return settings.get("c8y_proxy_url", "http://127.0.0.1:8001/c8y").rstrip("/")


def _http_json(url, method="GET", payload=None, timeout=20):
    """Small JSON helper over urllib (no third-party deps)."""
    headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def _resolve_source_id(settings):
    """Find this device's managed-object id via the c8y proxy.

    The external id can be set explicitly (settings["c8y_device_external_id"]);
    otherwise it is discovered from the proxy's authenticated device user, whose
    name is `device_<externalId>` for thin-edge certificate-based devices.
    """
    global _source_id_cache
    if _source_id_cache:
        return _source_id_cache

    base = _proxy_base(settings)
    ext_type = settings.get("c8y_external_id_type", "c8y_Serial")
    ext_id = settings.get("c8y_device_external_id")

    if not ext_id:
        user_name = _http_json(f"{base}/user/currentUser").get("userName", "")
        ext_id = user_name[len("device_"):] if user_name.startswith("device_") else user_name
    if not ext_id:
        raise RuntimeError("could not determine device external id from c8y proxy")

    ident = _http_json(f"{base}/identity/externalIds/{ext_type}/{ext_id}")
    _source_id_cache = ident["managedObject"]["id"]
    return _source_id_cache


def _attach_binary(base, event_id, img_bytes, img_filename, timeout=20):
    """Attach a JPEG to an existing event as multipart/form-data."""
    boundary = "----visiondemo" + uuid.uuid4().hex
    meta = json.dumps({"name": img_filename, "type": "image/jpeg"}).encode("utf-8")
    b = boundary.encode("utf-8")
    body = b"".join([
        b"--", b, b"\r\n",
        b'Content-Disposition: form-data; name="object"\r\n',
        b"Content-Type: application/json\r\n\r\n", meta, b"\r\n",
        b"--", b, b"\r\n",
        b'Content-Disposition: form-data; name="file"; filename="',
        img_filename.encode("utf-8"), b'"\r\n',
        b"Content-Type: image/jpeg\r\n\r\n", img_bytes, b"\r\n",
        b"--", b, b"--\r\n",
    ])
    req = urllib.request.Request(
        f"{base}/event/events/{event_id}/binaries",
        data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    urllib.request.urlopen(req, timeout=timeout).read()


def _publish_event_with_image(event_type, text, fragment, img_bytes, img_filename,
                                mqtt_client, settings):
    """
    Publish a Cumulocity event with an attached image via thin-edge's local
    Cumulocity HTTP proxy (no `tedge` CLI needed): create the event, then attach
    the JPEG. Falls back to a plain MQTT event (without image) on any failure.
    """
    if img_bytes is None:
        log.warning("No image to attach — falling back to MQTT")
        mqtt_client.publish_event(event_type, text, fragment)
        return

    try:
        base = _proxy_base(settings)
        source_id = _resolve_source_id(settings)
        event = _http_json(
            f"{base}/event/events/",
            method="POST",
            payload={
                "source": {"id": source_id},
                "type": event_type,
                "text": text,
                "time": datetime.now(timezone.utc).isoformat(),
                event_type: fragment,
            },
        )
        _attach_binary(base, event["id"], img_bytes, img_filename)
        log.info(f"Event uploaded with image ({len(img_bytes) // 1024} KB) via c8y proxy")

    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200] if hasattr(e, "read") else ""
        log.warning(f"c8y proxy upload failed (HTTP {e.code}): {detail} — falling back to MQTT")
        mqtt_client.publish_event(event_type, text, fragment)
    except Exception as e:
        log.warning(f"c8y proxy upload failed ({e}) — falling back to MQTT")
        mqtt_client.publish_event(event_type, text, fragment)


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
