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
./otc_capture --outdir captures --ip 192.168.0.101 --serial 26054106  # direct, no scan
./otc_capture --help
```

### Options

| Option | Default | Meaning |
|--------|---------|---------|
| `--serial N` | `0` (first found) | Camera serial number |
| `--network CIDR` | `192.168.0.0/24` | Ethernet subnet to scan (discovery) |
| `--ip ADDR` | — | Connect directly to this camera IP, skipping discovery (requires `--serial`) |
| `--port N` | `50101` | Local UDP port the camera streams to |
| `--outdir DIR` | `./captures` | Output directory |
| `--count N` | `1` | Number of frames to capture |
| `--interval-ms MS` | `1000` | Delay between captures |
| `--timeout-s S` | `30` | How long to wait for valid data |
| `--csv` | off | Also write per-pixel °C as CSV |
| `--raw` | off | Also write raw `float32` (`.f32`) |

#### Direct connect vs. discovery (`--ip`)

By default the tool **discovers** the camera by scanning `--network` (a UDP
broadcast enumeration over that subnet) and then connects by serial. `--ip`
instead connects **directly** to a known camera address and skips discovery
entirely — it just needs `--serial` too (the SDK connects directly only when a
serial is supplied; a zero serial forces enumeration).

This matters for containerized/routed setups: broadcast discovery can't cross a
NAT'd Docker bridge, which is one reason the Compose setup uses host networking.
With `--ip` the capture reaches a routable camera address without broadcast, so
the pipeline can run on a non-host network (e.g. a macvlan or routed L3 path)
where only unicast to the camera works.

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

## Run the whole thing with Docker Compose

One image, one service, two processes: a long-lived `otc_capture` holding a
single connection to the camera open and writing a frame every
`capture_interval_ms`, and the
[onnx-pipeline-runner](https://github.com/Cumulocity-IoT/onnx-pipeline-runner)
loop, whose preprocessor reads the newest frame off disk and publishes to
Cumulocity. The camera admits only one client, hence the split.

Holding the connection open is what keeps the shutter quiet: reconnecting per
cycle made the SDK run a startup NUC every time — audible flag, ~20 s before
valid data. Held open, the flag closes only on the camera's own NUC schedule.

There are three compose files:

| File | What it does |
|---|---|
| `docker-compose.yml` | **Pulls the pre-built image** from GHCR (`ghcr.io/mstoffel-sag/vision-demo:latest`, published by CI on each `v*` release). Default. |
| `docker-compose.build.yml` | **Overrides `docker-compose.yml` to build locally** from source (compiles `otc_capture` against the Optris SDK, builds the ONNX model). Carries only the build stanza — pass both files. |
| `docker-compose_vision_demo.yml` | **Deploys to a device via Cumulocity** as a thin-edge.io `container-group` software item. Same services and mounts as `docker-compose.yml`; see [below](#deploying-via-cumulocity). |

```bash
# Run the latest published image (fetches it online):
docker compose up -d

# …or build everything locally from source instead:
cp .env.example .env          # set the Optris SDK version
docker compose -f docker-compose.yml -f docker-compose.build.yml up --build
```

The GHCR package inherits the repo's visibility; if it is private, run
`docker login ghcr.io` first. Override the pulled image with the `PIPELINE_IMAGE`
env var (e.g. pin a tag: `PIPELINE_IMAGE=ghcr.io/mstoffel-sag/vision-demo:0.0.5`).

### Deploying via Cumulocity

Upload `docker-compose_vision_demo.yml` as the artifact of a `container-group`
software item. tedge-container-plugin materializes it into its own compose
directory and brings the project up.

> **It is regenerated from the artifact on every install/update**, so edits made
> directly on the device are silently discarded. Change the file here and
> re-upload — it is the source of truth, and must stay in step with
> `docker-compose.yml`. Its header lists the deliberate differences.

#### The image tag is pinned, on purpose

The artifact names an explicit release (`…/vision-demo:0.0.9`), not `:latest`.
A device resolves an image reference it already holds to the copy stored
locally, so with a floating tag an in-place update changes nothing: identical
reference, no pull, install reports success while the old image keeps running.
Naming a version the device lacks forces the pull — and makes rollback
expressible, which `:latest` cannot represent at all.

**The tag has no leading `v`.** CI uses docker/metadata-action
`type=semver,pattern={{version}}`, so git tag `v0.0.9` publishes `0.0.9`, `0.0`
and `latest`; pinning `:v0.0.9` fails with `manifest unknown`.

So a release is two steps in one commit — bump the pin, then tag:

```bash
git tag -a v0.1.0 -m "..." && git push origin v0.1.0
```

On a `v*` tag, `scripts/check-compose-sync.py --expect-tag` fails the build if
the artifact pins a different version than the tag, and `release` depends on it.
Without that guard a release would publish an image no device is asked to
install. `docker-compose.yml` keeps `:latest` deliberately — it is the dev file
and pairs the floating tag with `pull_policy: always`.

> **Note on disk.** CI has no layer cache, so each release shares no layers with
> the previous one and the pull needs room for the whole image. On a
> space-constrained device, remove the software item and reinstall rather than
> updating in place — removal drops the old image first.

### What you need first

- **thin-edge.io running on the host**, connected to your Cumulocity tenant —
  this compose does *not* bootstrap it. Host networking gives the container its
  MQTT broker on `localhost:1883` and its Cumulocity HTTP proxy on
  `localhost:8001`. The postprocessor calls that proxy directly (create event →
  attach JPEG), so no `tedge` CLI or device certificate is needed in the
  container — the proxy injects auth on the host. Override with `c8y_proxy_url`
  / `c8y_device_external_id` in `pipeline.json` if the auto-detected values are
  wrong. If the proxy is unreachable, alerts still publish over MQTT, without
  the image.
- The **Optris camera on the same Ethernet subnet** as the host. The pipeline
  container uses `network_mode: host`, so it scans the host's interfaces —
  set `capture_network` / `camera_serial` in `pipeline/config/pipeline.json`
  to match your camera.
- **Only when building locally** (`docker-compose.build.yml`): the Dockerfile
  downloads `otcsdk-<version>-ubuntu-<ubuntu>-<arch>.deb` from
  [Optris' GitHub releases](https://github.com/Optris/otcsdk_downloads/releases)
  — pick the version in `.env`. The pulled image already bundles it.

### Configuration

| Where | What |
|---|---|
| `.env` | Optris SDK version + Ubuntu base — **build only** (`docker-compose.build.yml`). |
| `PIPELINE_IMAGE` env | Override the pulled image/tag (`docker-compose.yml`). |
| `pipeline/config/pipeline.json` | The live use-case config — device/equipment info, `capture_network`, `temp_threshold_celsius`, and `mqtt_host`/`mqtt_port` (**point these at your thin-edge broker, localhost**). |
| `pipeline/processors/*.py` | Pre/post-processors. |

`pipeline.json` and both processors are **bind-mounted** and hot-reloaded each
cycle — edit on the host and the change applies without a rebuild or re-pull.
The compiled `otc_capture` and `model.onnx` are baked into the image; changing
`otc_capture.cpp` needs a rebuild
(`docker compose -f docker-compose.yml -f docker-compose.build.yml build`) or a
new release.

> **Model ↔ resolution coupling.** The baked `model.onnx` is built for the
> default 384×240 frame and 6×8 grid, which must match `frame_width`,
> `frame_height`, `grid_rows` and `grid_cols` in `pipeline.json` — change them
> and inference fails on the input shape. Build a matching model and mount it
> over the baked one instead of rebuilding the image (each compose file has a
> commented `model.onnx` volume for this; the runner hot-reloads it):
>
> ```bash
> python3 pipeline/build_thermal_model.py --height H --width W \
>     --grid-rows R --grid-cols C --output pipeline/model.onnx
> ```

### Multi-arch

BuildKit selects the SDK `.deb` and Python wheels for the target architecture.
Building natively on a 64-bit Raspberry Pi (`aarch64`) just works; from an x86
machine, cross-build with `docker buildx build --platform linux/arm64 ...`.

### Released image

Pushing a `v*` tag publishes a multi-arch (amd64 + arm64) image to GHCR at
`ghcr.io/mstoffel-sag/vision-demo` (see
[.github/workflows/build.yml](.github/workflows/build.yml)), which is what
`docker-compose.yml` pulls. Pin a specific release with `PIPELINE_IMAGE`.

> **Watching it run:** `docker compose logs -f pipeline` shows one line per
> cycle (`Cycle N | NORMAL/ALERT | pre=… inf=… post=…`). If the runner can't
> reach the thin-edge MQTT broker at startup it exits and restarts — check that
> thin-edge is running on the host and `mqtt_host`/`mqtt_port` are correct.

---

## Cumulocity thermal-alert pipeline (optional)

`pipeline/` turns `otc_capture` into a monitoring service: on a fixed interval it
captures a frame, runs it through a tiny ONNX model, and — if any grid cell
exceeds a configured temperature threshold — raises a Cumulocity alarm and
uploads an annotated snapshot as a `c8y_ThermalAlert` event.

The alarm is republished every alerting cycle (Cumulocity updates the open one
rather than stacking duplicates), but the **snapshot is uploaded once per alarm
episode** — a hot spot that stays hot is one open alarm with one picture, not a
JPEG every 30 s. Before uploading, the postprocessor asks Cumulocity whether an
alarm of `c8y_alarm_type` is still open (`ACTIVE` or `ACKNOWLEDGED`), rather
than trusting a local flag: a container restart mid-alarm then skips the
duplicate, and clearing the alarm in the UI while the spot is still hot yields a
fresh picture. If Cumulocity is unreachable it falls back to in-process state.
Set `alert_image_once_per_alarm` to `false` to upload on every alerting cycle.

It runs on top of **[tedge-pipeline-runner](https://github.com/Cumulocity-IoT/onnx-pipeline-runner)**,
a generic `Preprocess → ONNX inference → Postprocess` engine for thin-edge.io.
That runner is generic infrastructure you install once; everything in
`pipeline/` is what plugs into it to make this specific Optris + Cumulocity
use case work.

| File | Purpose |
|------|---------|
| `pipeline/config/pipeline.json` | Device/equipment info, capture settings, alert threshold. |
| `pipeline/processors/preprocessor.py` | Loads the newest `_temp.csv` written by the capture process into a tensor. |
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
- `python3-numpy` and `python3-pil` on the device (the runner's `.deb` pulls in
  numpy; without Pillow, alerts publish without the attached image):
  ```bash
  sudo apt install python3-numpy python3-pil
  ```

### Deploying the pipeline files

**Production (recommended):** push the four files below via Cumulocity's
**Configuration** tab on the device — no SSH needed. Get `model.onnx` from the
[latest Release](../../releases/latest), or build it yourself (above):

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
| `capture_interval_ms` | How often the held-open capture process writes a frame (default 5000). Not the pipeline cycle rate — that is `capture_interval_sec`. |
| `frame_max_age_s` | Fail the cycle rather than infer on a frame older than this (default 120), so a dead capture process surfaces instead of replaying a stale frame. |
| `frame_width` / `frame_height` | Must match the camera's native resolution and the resolution `model.onnx` was built for (384×240 for the Xi410 above). |
| `temp_threshold_celsius` | Grid-cell mean temperature (°C) that triggers an alert. |
| `equipment_id`, `equipment_name`, `location`, `camera_model` | Attached to every alert event/alarm. |
| `c8y_event_type`, `c8y_alarm_type`, `c8y_alarm_severity` | Cumulocity event/alarm types raised on alert. |
| `alert_image_once_per_alarm` | `true` (default): upload one alert image per alarm episode. `false`: upload one every alerting cycle. |

> The `capture_*` and `camera_*` settings configure the long-lived capture
> process, so they are read at **container start only** — restart the container
> after changing one. Everything else hot-reloads per cycle.

If you change `frame_width`/`frame_height` or the alert grid resolution,
rebuild the model to match:

```bash
python3 pipeline/build_thermal_model.py --height 240 --width 384 \
    --grid-rows 6 --grid-cols 8 --output pipeline/model.onnx
```

### How an alert looks

Each cycle, `preprocessor.py` reads the newest per-pixel temperature CSV
written by the capture process and feeds it into `model.onnx`. If any grid
cell's average exceeds `temp_threshold_celsius`, `postprocessor.py`:

1. Raises a `c8y_ThermalAlarm`, republished each alerting cycle and cleared
   automatically once the temperature drops back below threshold.
2. Renders the frame with Pillow (`inferno` colormap scaled to that frame's own
   1st/99th-percentile temperatures, so the background stays visible instead of
   clipping to black) with a red box over the hottest grid cell, and uploads it
   as a `c8y_ThermalAlert` event through thin-edge's Cumulocity HTTP proxy —
   once per alarm episode, see above.

---

## Troubleshooting

- **`Timed out waiting for valid thermal data`** — camera not reachable or wrong
  subnet. Run `otc_find_devices -e -a <your-cidr>` and pass the right
  `--network`. Check `ping <camera-ip>`.
- **First run is slow / logs "acquiring calibration files"** — normal; the SDK
  fetches calibration from the camera once and caches it in `~/.config/optris/`.
- **`error while loading shared libraries: libotcsdk.so`** — the SDK isn't
  installed (or not in the loader path). Install the `.deb`, then `sudo ldconfig`.
- **Python bindings segfault** — the SDK's Python binding (`import
  optris.otcsdk`) crashes in `Sdk.init()` under Python 3.14. The native C++ path
  used here is unaffected; run Python under the version the binding was built
  against.
- **`otc_capture` PNG is a flat, near-uniform color** — the `ImageBuilder`
  auto-scaling filter seeds from a hardcoded `-20..20 °C` range and converges
  slowly, crushing detail for scenes outside it. `otc_capture.cpp` already sets
  `Sigma3` scaling and disables the filter
  (`setTemperatureScalingFilterFactor(0.0f)`); if you still see it, you are
  running an older prebuilt binary.
- **`--fast-start` gives wildly wrong absolute temperatures** — it skips the
  startup NUC, at the documented cost of accuracy: the same static scene read
  ~120 °C with it vs. ~25 °C without. Use it only as a "is the camera
  connected" check, never where absolute temperatures matter (including the
  `pipeline/` alert threshold).
- **Cumulocity alert image background is solid black** — an old
  `postprocessor.py` with a fixed color range that sat above ambient, clipping
  the background. The current one scales per frame. Confirm the deployed
  `/opt/tedge-pipeline/processors/postprocessor.py` matches this repo's copy
  (pushed config and manual `scp` drift easily) and restart the service.
- **Device claim: `busy with another client`** — something else holds the
  camera's connection (a viewer app, another workstation, an unclean exit);
  `otc_find_devices` reporting `available` doesn't mean the SDK's claim tracking
  agrees. Close it, or power-cycle the camera to clear a stuck claim.
