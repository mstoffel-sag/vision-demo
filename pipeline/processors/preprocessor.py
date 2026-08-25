"""
Optris Thermal Preprocessor — reads the newest live frame written by the
long-lived otc_capture process and feeds it into the ONNX model.

Interface contract (required by tedge-pipeline-runner):
    get_input(config, cycle_count) -> dict with "input" and "metadata"

Data source:
    A single long-lived `otc_capture` process (started by the container
    entrypoint, see docker/entrypoint.sh) holds one connection to the camera
    open and writes a frame every `capture_interval_ms`:
        <prefix>.png       false-color picture
        <prefix>_temp.csv  per-pixel temperature in °C (h rows x w cols)

    This preprocessor does NOT connect to the camera — it only reads the newest
    *_temp.csv off disk. That split is deliberate: the camera admits a single
    client, and reconnecting per cycle made the SDK run a startup NUC every
    time (audible shutter, ~20s of dead time before valid data). One held-open
    connection means the flag only closes on the camera's own NUC schedule.

Settings (config["settings"]):
    frame_max_age_s       int  - reject frames older than this; guards against
                                 feeding a stale frame to the model when the
                                 capture process has died or the camera dropped
                                 (default: 120)
    frame_wait_timeout_s  int  - how long to wait for a usable frame before
                                 giving up. The first cycle after a restart
                                 waits out the SDK's startup NUC (~20s), so
                                 keep this comfortably above that (default: 90)
    keep_frames           int  - how many past capture sets to retain on disk
                                 (default: 5)
    frame_width/height    int  - must match the camera's native resolution the
                                 ONNX model was built for (see build_thermal_model.py)

    Camera settings (capture_binary, camera_ip, camera_port, camera_serial,
    capture_network, capture_timeout_s, capture_interval_ms) are consumed by
    the entrypoint at container start, not here — changing one needs a
    container restart, not just a config hot-reload.
"""

import time
import numpy as np
from pathlib import Path
from datetime import datetime, timezone

# A CSV whose mtime is younger than this may still be mid-write. otc_capture
# writes the PNG first and the CSV last, so a fresh CSV is the in-progress one.
_SETTLE_S = 1.5


def _resize_nearest(arr, out_h, out_w):
    """Nearest-neighbor resize, no extra dependencies (PIL/scipy not guaranteed on-device)."""
    in_h, in_w = arr.shape
    if (in_h, in_w) == (out_h, out_w):
        return arr
    row_idx = (np.arange(out_h) * in_h // out_h).astype(int)
    col_idx = (np.arange(out_w) * in_w // out_w).astype(int)
    return arr[row_idx][:, col_idx]


def _capture_dir(config):
    d = Path(config.get("data_dir", "/opt/tedge-pipeline/data")) / "captures"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _try_load(csv_path):
    """Load a *_temp.csv, or return None if it is not a complete 2-D frame yet."""
    try:
        matrix = np.loadtxt(csv_path, delimiter=",", dtype=np.float32)
    except (ValueError, OSError):
        return None          # ragged/truncated rows: the writer is mid-file
    return matrix if matrix.ndim == 2 else None


def _newest_settled_frame(out_dir):
    """Newest fully-written *_temp.csv as (path, matrix, age_s), or None if none yet."""
    now = time.time()
    csvs = sorted(out_dir.glob("*_temp.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    for csv_path in csvs:
        age = now - csv_path.stat().st_mtime
        if age < _SETTLE_S:
            continue         # still being written; try the one before it
        matrix = _try_load(csv_path)
        if matrix is not None:
            return csv_path, matrix, age
    return None


def _wait_for_frame(out_dir, max_age_s, timeout_s):
    """
    Poll until a frame newer than `max_age_s` is available; return (path, matrix, age).

    A too-old frame is treated as "not ready yet" rather than an immediate
    failure: the captures volume persists across restarts, so right after one
    the newest frame on disk is a leftover from the previous run, and the
    capture process needs its startup NUC (~20s) before writing a fresh one.

    The flip side is that a genuinely dead capture process costs a full
    `timeout_s` per cycle before the error surfaces. That is the intended
    trade — the alternative is inferring on a stale frame and raising alarms
    from temperatures that no longer exist.
    """
    deadline = time.monotonic() + timeout_s
    stale_age = None
    while True:
        found = _newest_settled_frame(out_dir)
        if found is not None:
            _, _, age = found
            if age <= max_age_s:
                return found
            stale_age = age
        if time.monotonic() > deadline:
            if stale_age is not None:
                raise RuntimeError(
                    f"newest thermal frame is {stale_age:.0f}s old (max {max_age_s}s) — "
                    f"the otc_capture process is not producing frames; check the container logs"
                )
            raise RuntimeError(
                f"no thermal frame appeared within {timeout_s}s — "
                f"is the otc_capture process running? check the container logs"
            )
        time.sleep(0.5)


def _prune_old_frames(out_dir, keep):
    """Keep only the newest `keep` capture sets (csv + matching png/f32) to bound disk usage."""
    csvs = sorted(out_dir.glob("*_temp.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    for csv_path in csvs[keep:]:
        prefix = csv_path.name[: -len("_temp.csv")]
        for sibling in out_dir.glob(prefix + "*"):
            sibling.unlink(missing_ok=True)


def get_input(config, cycle_count):
    """
    Read the newest live thermal frame and prepare it for ONNX inference.

    Args:
        config: dict — full pipeline config (including config["settings"])
        cycle_count: int — current cycle number (1-based)

    Returns:
        dict:
            "input": np.ndarray float32 [1, 1, H, W] — model-ready tensor
            "metadata": dict — passed through to postprocessor
    """
    settings = config.get("settings", {})
    out_dir = _capture_dir(config)

    csv_path, temp_matrix, frame_age_s = _wait_for_frame(
        out_dir,
        int(settings.get("frame_max_age_s", 120)),
        int(settings.get("frame_wait_timeout_s", 90)),
    )

    frame_h = int(settings.get("frame_height", temp_matrix.shape[0]))
    frame_w = int(settings.get("frame_width", temp_matrix.shape[1]))
    temp_matrix = _resize_nearest(temp_matrix, frame_h, frame_w)

    _prune_old_frames(out_dir, int(settings.get("keep_frames", 5)))

    input_tensor = temp_matrix[np.newaxis, np.newaxis, :, :]

    return {
        "input": input_tensor,
        "metadata": {
            "timestamp": datetime.now(timezone.utc),
            "frame_id": csv_path.stem,
            "source_file": str(csv_path),
            "frame_age_s": round(frame_age_s, 1),
            "temp_matrix": temp_matrix,
            "frame_min": float(temp_matrix.min()),
            "frame_max": float(temp_matrix.max()),
            "frame_mean": float(temp_matrix.mean()),
        },
    }
