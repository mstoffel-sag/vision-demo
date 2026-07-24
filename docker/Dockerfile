# syntax=docker/dockerfile:1
###############################################################################
# vision-demo pipeline image
#
# One container that performs BOTH halves of the workflow:
#   1. Capture  — the compiled `otc_capture` tool grabs a thermal frame from
#                 the Optris camera over Ethernet (driven per-cycle by the
#                 preprocessor; the camera only allows one client, so capture
#                 is not a separate service).
#   2. ONNX     — the onnx-pipeline-runner loop:
#                 preprocess -> ONNX inference -> postprocess, publishing
#                 measurements/events/alarms to an *external* thin-edge.io
#                 container over MQTT (and alert images over its HTTP proxy).
#
# Multi-arch: BuildKit sets TARGETARCH (amd64 | arm64); the Optris SDK .deb and
# the Python wheels are selected to match. Build for the Pi with:
#   docker buildx build --platform linux/arm64 ...   (or just `docker compose
#   build` natively on the Pi).
###############################################################################

ARG UBUNTU_VERSION=24.04

# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — builder: Optris SDK + g++ to compile otc_capture, and the ONNX model
# ─────────────────────────────────────────────────────────────────────────────
FROM ubuntu:${UBUNTU_VERSION} AS builder

ARG TARGETARCH
ARG OTCSDK_VERSION=11.3.0
ARG OTCSDK_UBUNTU=24.04
# Full .deb filename — override this single arg if Optris' arch naming ever
# differs from Docker's TARGETARCH (amd64 / arm64).
ARG OTCSDK_DEB=otcsdk-${OTCSDK_VERSION}-ubuntu-${OTCSDK_UBUNTU}-${TARGETARCH}.deb
ARG OTCSDK_URL=https://github.com/Optris/otcsdk_downloads/releases/download/v${OTCSDK_VERSION}/${OTCSDK_DEB}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl build-essential zlib1g-dev \
        python3 python3-numpy \
    && rm -rf /var/lib/apt/lists/*

# Optris Thermal Camera SDK — provides otcsdk headers + libotcsdk + CLI tools.
# `apt install ./file.deb` resolves the SDK's own runtime dependencies.
# Keep the .deb around so the runtime stage can reuse it (copied via
# `COPY --from=builder`) instead of re-downloading — no curl needed at runtime.
RUN curl -fSL -o /opt/otcsdk.deb "$OTCSDK_URL" \
    && apt-get update \
    && apt-get install -y --no-install-recommends /opt/otcsdk.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
# Compile the native capture tool (same command as build.sh).
COPY otc_capture.cpp ./
RUN g++ -std=c++17 -O2 -Wall otc_capture.cpp -o otc_capture -lotcsdk -lz

# Build the thermal feature-extraction model to match the camera resolution
# and grid used in pipeline/config/pipeline.json (384x240, 6x8 grid).
COPY pipeline/build_thermal_model.py ./
RUN python3 build_thermal_model.py \
        --height 240 --width 384 --grid-rows 6 --grid-cols 8 \
        --output /src/model.onnx

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — runtime
# ─────────────────────────────────────────────────────────────────────────────
FROM ubuntu:${UBUNTU_VERSION} AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Python stack. numpy/pillow come from apt (prebuilt, avoids slow source builds
# on arm64); onnxruntime + paho-mqtt from pip (manylinux wheels exist for both
# amd64 and arm64). Alert images are rendered with Pillow (no matplotlib).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 python3-pip python3-numpy python3-pil \
    && pip3 install --no-cache-dir --break-system-packages onnxruntime paho-mqtt \
    && apt-get purge -y python3-pip \
    && apt-get autoremove -y --purge \
    && rm -rf /var/lib/apt/lists/*

# Optris SDK runtime.
#
# The .deb declares a full build toolchain (gcc, g++, cmake, build-essential),
# -dev header packages, and an OpenGL dev stack (freeglut3-dev, libglew-dev,
# which pull in mesa + llvm) as hard Depends. None of that is needed to *run*
# the already-compiled otc_capture: `ldd otc_capture` / `ldd libotcsdk.so` show
# they link only libusb-1.0 + libudev (plus base glibc/libstdc++/z). So rather
# than `apt install ./otcsdk.deb` — which drags in ~450MB of gcc/cmake/mesa/llvm
# — extract the SDK's files directly (libs, CLI tools, and the norm/*.prn
# calibration data otc_capture needs) and install just the two real runtime
# libraries. Drop the static lib and bundled PDF, which serve no runtime purpose.
# The .deb is bind-mounted from the builder, so it never becomes an image layer.
RUN --mount=type=bind,from=builder,source=/opt/otcsdk.deb,target=/tmp/otcsdk.deb \
    apt-get update \
    && apt-get install -y --no-install-recommends libusb-1.0-0 libudev1 \
    && dpkg -x /tmp/otcsdk.deb / \
    && rm -f /usr/lib/libotcsdk_static.a '/usr/share/otcsdk/Quick Start Guide.pdf' \
    && ldconfig \
    && rm -rf /var/lib/apt/lists/*

# Generic ONNX pipeline engine (checked into this repo).
COPY docker/pipeline_runner.py /opt/tedge-pipeline/pipeline_runner.py

# Compiled capture tool + built model from the builder stage.
COPY --from=builder /src/otc_capture       /usr/local/bin/otc_capture
COPY --from=builder /src/model.onnx        /opt/tedge-pipeline/models/model.onnx

# Use-case files. Baked in as defaults; docker-compose also bind-mounts them so
# host-side edits hot-reload (mirrors the Cumulocity Configuration-push flow).
COPY pipeline/config/pipeline.json         /opt/tedge-pipeline/config/pipeline.json
COPY pipeline/processors/preprocessor.py   /opt/tedge-pipeline/processors/preprocessor.py
COPY pipeline/processors/postprocessor.py  /opt/tedge-pipeline/processors/postprocessor.py

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/otc_capture \
    && mkdir -p /opt/tedge-pipeline/data/captures

WORKDIR /opt/tedge-pipeline
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
