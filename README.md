# Optris Thermal Camera — Ethernet Capture

Capture thermal images from an Optris camera (Xi410 / Xi400 / Xi640 / PI series)
over **Ethernet** on Linux, using the native **Optris Thermal Camera SDK**
(`otcsdk`, libotcsdk). No Python required.

Each capture produces:

| File | Contents |
|------|----------|
| `optris_<timestamp>_<n>.png` | False-color thermal picture (RGB, 8-bit) |
| `optris_<timestamp>_<n>_temp.csv` | Per-pixel temperature in °C (with `--csv`) |
| `optris_<timestamp>_<n>_temp.f32` | Raw little-endian `float32` temperatures (with `--raw`) |

Verified against a **Xi410** (S/N 26054106) at `192.168.0.101:50101`,
384×240 @ 25 Hz.

---

## Files

| File | Purpose |
|------|---------|
| `otc_capture.cpp` | Native capture tool (connect → wait for shutter flag → save). |
| `build.sh` | Compiles `otc_capture.cpp` → `otc_capture`. |
| `otc_capture` | Compiled binary you run directly. |
| `captures/` | Output images and temperature data. |
| `pipeline/` | Optional Cumulocity thermal-alert pipeline — see [below](#cumulocity-thermal-alert-pipeline-optional). |

---

## Requirements

- Linux with the **Optris Thermal Camera SDK** installed
  (provides `/usr/include/otcsdk/`, `/usr/lib/libotcsdk.so`, and the
  `otc_find_devices` / `otc_version` CLI tools).
- `g++` (C++17) and `zlib` development headers.
- The camera reachable on the network (same subnet as the host).

```bash
sudo apt install build-essential zlib1g-dev   # compiler + zlib
```

The SDK itself is distributed by Optris as a `.deb` package — see
[github.com/Optris/otcsdk_downloads](https://github.com/Optris/otcsdk_downloads/releases).

---

## Build

```bash
./build.sh
```

This compiles `otc_capture.cpp` into the `otc_capture` binary:

```bash
g++ -std=c++17 -O2 -Wall otc_capture.cpp -o otc_capture -lotcsdk -lz
```

You only need to rebuild when `otc_capture.cpp` changes. The binary is
architecture-specific — recompile on each target (e.g. separately on a
Raspberry Pi; see below).

## Running

```bash
# 1. Confirm the camera is on the network:
otc_find_devices -e -a 192.168.0.0/24

# 2. Capture (create the output directory first — the tool does not create it):
mkdir -p captures
./otc_capture --outdir captures                    # one snapshot
```

> **Note:** `otc_capture` writes into `--outdir` but does not create it. If the
> directory is missing the run reports success but silently writes no files, so
> `mkdir -p` it first (default is `./captures`).

You can install it on your `PATH` to run from anywhere:

```bash
sudo cp otc_capture /usr/local/bin/
```

### More examples

```bash
./otc_capture --outdir captures --count 10 --interval-ms 2000  # 10 shots, 2 s apart
./otc_capture --outdir captures --csv --raw                    # also dump temperature data
./otc_capture --outdir captures --serial 26054106              # target a specific camera
./otc_capture --outdir captures --network 10.0.0.0/24          # a different subnet
./otc_capture --help
```

### Options

| Option | Default | Meaning |
|--------|---------|---------|
| `--serial N` | `0` (first found) | Camera serial number |
| `--network CIDR` | `192.168.0.0/24` | Ethernet subnet to scan |
| `--outdir DIR` | `./captures` | Output directory |
| `--count N` | `1` | Number of frames to capture |
| `--interval-ms MS` | `1000` | Delay between captures |
| `--timeout-s S` | `30` | How long to wait for valid data |
| `--csv` | off | Also write per-pixel °C as CSV |
| `--raw` | off | Also write raw `float32` (`.f32`) |

### Reading the raw temperature file

`.f32` is a flat little-endian `float32` array in row-major order
(width × height, °C):

```python
import numpy as np
temps = np.fromfile("captures/optris_..._0_temp.f32", dtype="<f4").reshape(240, 384)
print(temps.max(), "°C hotspot")
```

---

## How it works

The camera streams over UDP. On first connect the SDK downloads the calibration
files from the camera and caches them in `~/.config/optris/` (this makes the
first run take a few seconds). The tool subclasses `IRImagerClient`, runs the
grabber asynchronously, waits for the shutter flag to reach `Open` (valid
thermal data), then converts each frame to a false-color image with
`ImageBuilder` and writes a PNG (encoded in-process via zlib). Temperatures come
straight from `ThermalFrame::copyTemperaturesTo()` in °C.

---

## Compiling for a Raspberry Pi

Optris ships **arm64** SDK builds, so a Raspberry Pi works — with one
requirement: the Pi must run a **64-bit OS** (arm64 / aarch64). Optris does
**not** provide a 32-bit `armhf` build, so 32-bit Raspberry Pi OS will not work.

Check with `uname -m` → it must report `aarch64`. (Pi 3/4/5 with the 64-bit
Raspberry Pi OS or Ubuntu arm64.)

### Recommended: build natively on the Pi

This is by far the simplest and most reliable approach — the exact same
`g++` command as on x86.

```bash
# 1. Install the arm64 SDK on the Pi (24.04 build works on Pi OS Bookworm / Ubuntu 24.04):
wget https://github.com/Optris/otcsdk_downloads/releases/download/v11.3.0/otcsdk-11.3.0-ubuntu-24.04-arm64.deb
sudo apt install ./otcsdk-11.3.0-ubuntu-24.04-arm64.deb   # pulls in libusb, libudev, etc.

# 2. Install the build dependencies:
sudo apt install build-essential zlib1g-dev

# 3. Copy this project to the Pi (the .cpp — do NOT reuse the x86 binary), then:
./build.sh
mkdir -p captures
./otc_capture --outdir captures
```

The `otc_capture` binary is architecture-specific, so always rebuild on the Pi
with `./build.sh` — don't copy over the x86 binary. Equivalent by hand:

```bash
g++ -std=c++17 -O2 otc_capture.cpp -o otc_capture -lotcsdk -lz
```

> If you are on **Raspberry Pi OS 22.04-era** or hit a GLIBC/library mismatch
> with the 24.04 build, use the `otcsdk-11.3.0-ubuntu-22.04-arm64.deb` asset
> instead.

### Alternative: cross-compile from an x86 machine

Native compilation on the Pi is recommended. Cross-compiling is only worth it
for CI or if the Pi is too slow to build on. You need an aarch64 toolchain **and**
the SDK's arm64 headers + libraries available as a sysroot (extract them from the
arm64 `.deb` with `dpkg-deb -x otcsdk-...-arm64.deb ./sysroot`):

```bash
sudo apt install g++-aarch64-linux-gnu

aarch64-linux-gnu-g++ -std=c++17 -O2 otc_capture.cpp -o otc_capture \
    --sysroot=./sysroot \
    -I./sysroot/usr/include \
    -L./sysroot/usr/lib -lotcsdk -lz
```

Then copy `otc_capture` to the Pi. You still need the runtime SDK installed on
the Pi (`sudo apt install ./otcsdk-...-arm64.deb`) so the shared libraries and
calibration tooling are present at run time. Because getting the sysroot and
library paths right is fiddly, prefer native builds unless you have a specific
reason not to.

### Networking note

However you build, make sure the Pi is on the same subnet as the camera and that
`--network` matches it (default `192.168.0.0/24`). Verify with:

```bash
otc_find_devices -e -a 192.168.0.0/24
```

---

## Cumulocity thermal-alert pipeline (optional)

`pipeline/` turns `otc_capture` into a monitoring service: on a fixed interval it
captures a frame, runs it through a tiny ONNX model, and — if any grid cell
exceeds a configured temperature threshold — raises a Cumulocity alarm and
uploads an annotated snapshot as a `c8y_ThermalAlert` event.

It runs on top of **[tedge-pipeline-runner](https://github.com/Cumulocity-IoT/onnx-pipeline-runner)**,
a generic `Preprocess → ONNX inference → Postprocess` engine for thin-edge.io.
That runner is generic infrastructure you install once; everything in
`pipeline/` is what plugs into it to make this specific Optris + Cumulocity
use case work.

| File | Purpose |
|------|---------|
| `pipeline/config/pipeline.json` | Device/equipment info, capture settings, alert threshold. |
| `pipeline/processors/preprocessor.py` | Runs `otc_capture` once per cycle, loads the `_temp.csv` into a tensor. |
| `pipeline/processors/postprocessor.py` | Applies the threshold, renders the annotated alert image, publishes to Cumulocity. |
| `pipeline/build_thermal_model.py` | Builds `model.onnx` — feature extraction only (smoothing + per-cell max/average), no threshold logic. |

`model.onnx` itself is **not** checked into the repo — it's built by CI from
`build_thermal_model.py` and attached to each [GitHub
Release](../../releases), so it's always in sync with that script. Grab it
from there, or build it yourself:

```bash
pip install numpy   # onnxruntime too, if you want the self-validation step
python3 pipeline/build_thermal_model.py --height 240 --width 384 \
    --grid-rows 6 --grid-cols 8 --output pipeline/model.onnx
```

### Prerequisites

- A device running [thin-edge.io](https://thin-edge.github.io/thin-edge.io/), connected to Cumulocity.
- `otc_capture` built and installed on that same device (see above) — the
  camera capture path this pipeline depends on.
- `tedge-pipeline-runner` installed on the device (see the
  [onnx-pipeline-runner Quick Start](https://github.com/Cumulocity-IoT/onnx-pipeline-runner#quick-start)):
  build `tedge-pipeline-runner_*.deb` from that repo, upload it to
  **Management > Software Repository** in Cumulocity, then install it on the
  device from its **Software** tab. This creates `/opt/tedge-pipeline/` and
  the `tedge-pipeline-runner` systemd service.
- `python3-numpy` and `python3-matplotlib` on the device (the runner's `.deb`
  installs these automatically; install manually only if you skip it):
  ```bash
  sudo apt install python3-numpy python3-matplotlib
  ```

### Deploying the pipeline files

**Production (recommended):** push the four files below via Cumulocity's
**Configuration** tab on the device — no SSH needed, and future updates work
the same way. Get `model.onnx` from the [latest
Release](../../releases/latest) (or build it yourself, above):

| Configuration Type | File to upload |
|---|---|
| `pipeline-config` | `pipeline/config/pipeline.json` |
| `pipeline-preprocessor` | `pipeline/processors/preprocessor.py` |
| `pipeline-postprocessor` | `pipeline/processors/postprocessor.py` |
| `pipeline-model` | `model.onnx` (from the Release, or built locally) |

**Manual (for local testing/dev boxes without Configuration Management set up):**

```bash
sudo install -m 0644 -o root -g root pipeline/config/pipeline.json      /opt/tedge-pipeline/config/pipeline.json
sudo install -m 0644 -o root -g root pipeline/processors/preprocessor.py /opt/tedge-pipeline/processors/preprocessor.py
sudo install -m 0644 -o root -g root pipeline/processors/postprocessor.py /opt/tedge-pipeline/processors/postprocessor.py
sudo install -m 0644 -o root -g root model.onnx                          /opt/tedge-pipeline/models/model.onnx

sudo systemctl restart tedge-pipeline-runner.service
sudo systemctl status tedge-pipeline-runner.service --no-pager
journalctl -u tedge-pipeline-runner.service -f    # watch a full cycle
```

### Before you deploy, edit `pipeline/config/pipeline.json`

The checked-in file has placeholder equipment info and paths — set at least:

| Setting | Meaning |
|---|---|
| `capture_binary` | Path to `otc_capture` (e.g. `/usr/local/bin/otc_capture`). |
| `capture_network` / `camera_serial` | Same as the `--network` / `--serial` capture options. |
| `frame_width` / `frame_height` | Must match the camera's native resolution and the resolution `model.onnx` was built for (384×240 for the Xi410 above). |
| `temp_threshold_celsius` | Grid-cell mean temperature (°C) that triggers an alert. |
| `equipment_id`, `equipment_name`, `location`, `camera_model` | Attached to every alert event/alarm. |
| `c8y_event_type`, `c8y_alarm_type`, `c8y_alarm_severity` | Cumulocity event/alarm types raised on alert. |

If you change `frame_width`/`frame_height` or the alert grid resolution,
rebuild the model to match:

```bash
python3 pipeline/build_thermal_model.py --height 240 --width 384 \
    --grid-rows 6 --grid-cols 8 --output pipeline/model.onnx
```

### How an alert looks

Each cycle, `preprocessor.py` runs `otc_capture --count 1 --csv` and feeds the
per-pixel temperature CSV into `model.onnx`. If any grid cell's average
exceeds `temp_threshold_celsius`, `postprocessor.py`:

1. Renders the full frame with `matplotlib` (`inferno` colormap, scaled to
   that frame's own 1st/99th-percentile temperatures — not a fixed range, so
   the background stays visible instead of clipping to black) with a red box
   and `+` marker over the hottest grid cell.
2. Uploads it via `tedge upload c8y` as a `c8y_ThermalAlert` event, and raises
   a `c8y_ThermalAlarm` that clears automatically once the temperature drops
   back below threshold.

---

## Troubleshooting

- **`Timed out waiting for valid thermal data`** — camera not reachable or wrong
  subnet. Run `otc_find_devices -e -a <your-cidr>` and pass the right
  `--network`. Check `ping <camera-ip>`.
- **First run is slow / logs "acquiring calibration files"** — normal; the SDK
  fetches calibration from the camera once and caches it in `~/.config/optris/`.
- **`error while loading shared libraries: libotcsdk.so`** — the SDK isn't
  installed (or not in the loader path). Install the `.deb`, then `sudo ldconfig`.
- **Python bindings segfault** — the SDK's Python 3 binding (`import
  optris.otcsdk`) crashes in `Sdk.init()` under Python 3.14 on this system. The
  native C++ path used here is unaffected. If you need Python, run it under the
  Python version the binding was built against, or report the crash to Optris.
- **`otc_capture` PNG is a flat, near-uniform color — no visible detail** — the
  `ImageBuilder` auto-scaling filter starts every frame from a hardcoded
  `-20..20 °C` seed range and only converges slowly (see `otc_capture.cpp`,
  `save_frame()`). For scenes well outside that range, or with a single
  outlier hot pixel, this crushes real detail into a thin sliver of the
  palette. `otc_capture.cpp` already sets `Sigma3` scaling and disables the
  filter (`setTemperatureScalingFilterFactor(0.0f)`) to avoid this — if you
  see it regardless, check you're running the binary built from the current
  `otc_capture.cpp`, not an older prebuilt one.
- **`--fast-start` gives wildly wrong absolute temperatures** — it skips the
  startup NUC/recalibration for a faster first frame, at the documented cost
  of accuracy. We measured the same static scene reading ~120 °C with
  `--fast-start` vs. ~25 °C without it. Don't use it for anything that reads
  absolute temperatures (including the `pipeline/` alert threshold) — it's
  only reasonable for a quick "is the camera even connected" sanity check.
- **Cumulocity alert image background is solid black** — this is a
  `postprocessor.py` rendering issue, not `otc_capture`: an earlier version
  used a fixed `vmin`/`vmax` color range that sat above typical ambient
  temperature, so `matplotlib` clipped the whole background to black and only
  the already-flagged hot region showed any color. The current version scales
  off each frame's own 1st/99th-percentile temperatures instead. If you still
  see this, confirm `/opt/tedge-pipeline/processors/postprocessor.py` matches
  `pipeline/processors/postprocessor.py` in this repo (Configuration
  Management pushes and manual `scp`/`cp` deploys are easy to let drift out of
  sync) and that the service was restarted after the update.
- **Device claim: `busy with another client`** — another client (another
  workstation, a viewer app, or an unclean previous exit) is already holding
  the camera's connection. `otc_find_devices` reporting `available` doesn't
  always mean the SDK's own claim tracking agrees. Close whatever else is
  connected; if nothing obvious is, power-cycling the camera clears a stuck
  claim.
