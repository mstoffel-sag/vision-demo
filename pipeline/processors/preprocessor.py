"""
Optris Thermal Preprocessor — captures a live frame via otc_capture and
feeds its per-pixel temperature CSV into the ONNX model.

Interface contract (required by tedge-pipeline-runner):
    get_input(config, cycle_count) -> dict with "input" and "metadata"

Data source:
    Runs the `otc_capture` binary (github.com/mstoffel-sag/vision-demo) once
    per cycle. It connects to the camera over Ethernet, waits for the shutter
    flag to open, and writes:
        <prefix>.png       false-color picture
        <prefix>_temp.csv  per-pixel temperature in °C (h rows x w cols)

    otc_capture must already be reachable — either on PATH or at the path
    given by settings["capture_binary"] — and the camera must be on the
    subnet given by settings["capture_network"].

Settings (config["settings"]):
    capture_binary        str   - path to the otc_capture executable (default: "otc_capture")
    capture_network       str   - Ethernet CIDR to scan (default: "192.168.0.0/24")
    camera_serial         int   - camera serial, 0 = first detected (default: 0)
    capture_timeout_s     int   - seconds to wait for valid thermal data (default: 30)
    keep_frames           int   - how many past capture sets to retain on disk (default: 5)
    frame_width/height    int   - must match the camera's native resolution the
                                  ONNX model was built for (see build_thermal_model.py)
"""

import subprocess
import numpy as np
from pathlib import Path
from datetime import datetime, timezone


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


def _run_capture(settings, out_dir):
    """Invoke otc_capture for exactly one frame; returns the written *_temp.csv path."""
    binary = settings.get("capture_binary", "otc_capture")
    timeout_s = int(settings.get("capture_timeout_s", 30))

    cmd = [
        binary,
        "--outdir", str(out_dir),
        "--network", settings.get("capture_network", "192.168.0.0/24"),
        "--serial", str(settings.get("camera_serial", 0)),
        "--timeout-s", str(timeout_s),
        "--count", "1",
        "--csv",
    ]

    before = {p.name for p in out_dir.glob("*_temp.csv")}

    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout_s + 15,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"otc_capture failed (code {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    new_files = sorted(
        (p for p in out_dir.glob("*_temp.csv") if p.name not in before),
        key=lambda p: p.stat().st_mtime,
    )
    if not new_files:
        raise RuntimeError("otc_capture reported success but wrote no *_temp.csv file")
    return new_files[-1]


def _prune_old_frames(out_dir, keep):
    """Keep only the newest `keep` capture sets (csv + matching png/f32) to bound disk usage."""
    csvs = sorted(out_dir.glob("*_temp.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    for csv_path in csvs[keep:]:
        prefix = csv_path.name[: -len("_temp.csv")]
        for sibling in out_dir.glob(prefix + "*"):
            sibling.unlink(missing_ok=True)


def get_input(config, cycle_count):
    """
    Capture a live thermal frame and prepare it for ONNX inference.

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

    csv_path = _run_capture(settings, out_dir)
    temp_matrix = np.loadtxt(csv_path, delimiter=",", dtype=np.float32)

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
            "temp_matrix": temp_matrix,
            "frame_min": float(temp_matrix.min()),
            "frame_max": float(temp_matrix.max()),
            "frame_mean": float(temp_matrix.mean()),
        },
    }
