#!/usr/bin/env bash
#
# Build the native Optris capture tool (otc_capture).
#
# Requirements:
#   - Optris Thermal Camera SDK installed (provides /usr/include/otcsdk + libotcsdk)
#   - g++ (C++17) and zlib headers:  sudo apt install build-essential zlib1g-dev
#
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$DIR/otc_capture.cpp"
BIN="$DIR/otc_capture"

echo "Compiling $SRC -> $BIN"
g++ -std=c++17 -O2 -Wall "$SRC" -o "$BIN" -lotcsdk -lz

echo "Build OK: $BIN"
