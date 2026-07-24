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
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

log = logging.getLogger("postprocessor")

if not HAS_PIL:
    log.warning(
        "Pillow not available — alert images will NOT be attached to events. "
        "Fix: sudo apt-get install -y python3-pil"
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


# matplotlib's "inferno" colormap, sampled at 11 evenly spaced anchor points
# (position 0.0 → 1.0, RGB in 0..1). np.interp between these reproduces the
# perceptually-uniform ramp closely enough for an alert thumbnail, without
# pulling in the whole matplotlib dependency. black → purple → magenta → orange → pale yellow.
_INFERNO_ANCHORS = np.array([
    [0.001462, 0.000466, 0.013866],
    [0.087411, 0.044556, 0.224813],
    [0.258234, 0.038571, 0.406485],
    [0.416331, 0.090203, 0.432943],
    [0.578304, 0.148039, 0.404411],
    [0.735683, 0.215906, 0.330245],
    [0.865006, 0.316822, 0.226055],
    [0.954506, 0.468744, 0.099874],
    [0.987622, 0.645320, 0.039886],
    [0.964394, 0.843848, 0.273391],
    [0.988362, 0.998364, 0.644924],
])
_INFERNO_POS = np.linspace(0.0, 1.0, len(_INFERNO_ANCHORS))


def _inferno_rgb(norm):
    """Map a normalized (0..1) 2-D array to an (H, W, 3) uint8 inferno image."""
    channels = [np.interp(norm, _INFERNO_POS, _INFERNO_ANCHORS[:, c]) for c in range(3)]
    return (np.stack(channels, axis=-1) * 255.0).astype(np.uint8)


def _load_font(size):
    """Scalable default font (Pillow >= 10 supports the size arg); fall back to
    the tiny bitmap default on older Pillow."""
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _render_annotated_image(metadata, bbox, max_temp, settings):
    """Render annotated thermal image as JPEG bytes (with bounding box and temperature label)."""
    if not HAS_PIL:
        return None

    temp_matrix = metadata.get("temp_matrix")
    if temp_matrix is None:
        return None

    temp_matrix = np.asarray(temp_matrix, dtype=np.float32)
    # Scale off this frame's own temperature spread (1st/99th percentile, robust to a
    # few outlier hot pixels) instead of a fixed range. A hardcoded vmin here previously
    # sat above typical ambient background temperature, so the whole background clipped
    # to solid black and only the already-flagged hot region showed any color.
    vmin = float(np.percentile(temp_matrix, 1))
    vmax = float(np.percentile(temp_matrix, 99))
    if vmax - vmin < 1.0:
        vmax = vmin + 1.0
    norm = np.clip((temp_matrix - vmin) / (vmax - vmin), 0.0, 1.0)

    img = Image.fromarray(_inferno_rgb(norm), "RGB")

    # Upscale the (small) thermal frame to a readable thumbnail; scale bbox/labels
    # to match. Target ~480px wide, mirroring the old matplotlib figure size.
    src_w, src_h = img.size
    scale = max(1.0, 480.0 / src_w)
    out_w, out_h = int(round(src_w * scale)), int(round(src_h * scale))
    img = img.resize((out_w, out_h), Image.BILINEAR)

    draw = ImageDraw.Draw(img, "RGBA")

    x0, y0 = bbox["topLeftX"] * scale, bbox["topLeftY"] * scale
    x1, y1 = bbox["bottomRightX"] * scale, bbox["bottomRightY"] * scale
    draw.rectangle([x0, y0, x1, y1], outline=(255, 0, 0, 255), width=2)

    # Center crosshair.
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    arm = 7
    draw.line([(cx - arm, cy), (cx + arm, cy)], fill=(255, 0, 0, 255), width=2)
    draw.line([(cx, cy - arm), (cx, cy + arm)], fill=(255, 0, 0, 255), width=2)

    # Temperature label on a red badge, centered above the box.
    label = f"{max_temp:.1f}°C"
    font = _load_font(16)
    tb = draw.textbbox((0, 0), label, font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    pad = 4
    lx = min(max(cx - tw / 2 - pad, 0), out_w - tw - 2 * pad)
    ly = max(y0 - th - 3 * pad, 0)
    draw.rectangle([lx, ly, lx + tw + 2 * pad, ly + th + 2 * pad],
                   fill=(220, 0, 0, 230))
    draw.text((lx + pad - tb[0], ly + pad - tb[1]), label,
              fill=(255, 255, 255, 255), font=font)

    # Title strip (equipment name - id) top-left, with a dark backing for legibility.
    title = f"{settings.get('equipment_name', '')} - {settings.get('equipment_id', '')}".strip(" -")
    if title:
        tfont = _load_font(13)
        ttb = draw.textbbox((0, 0), title, font=tfont)
        ttw, tth = ttb[2] - ttb[0], ttb[3] - ttb[1]
        draw.rectangle([0, 0, ttw + 8, tth + 8], fill=(0, 0, 0, 140))
        draw.text((4 - ttb[0], 4 - ttb[1]), title,
                  fill=(220, 220, 220, 255), font=tfont)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return buf.read()
