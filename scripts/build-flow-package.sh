#!/bin/sh
# Package a thin-edge.io flow directory as the .tar.gz the `flow` sm-plugin
# installs, and print the values needed to register it in Cumulocity.
#
#   scripts/build-flow-package.sh [flow-dir] [out-dir]
#
# The plugin unpacks the archive INTO <flows-dir>/<flow-name>/, so the flow
# files must sit at the archive root — not under a directory of their own.
set -eu

FLOW_DIR="${1:-flows/relay-auto-open}"
OUT_DIR="${2:-dist}"

[ -f "$FLOW_DIR/flow.toml" ] || { echo "no flow.toml in $FLOW_DIR" >&2; exit 1; }

NAME=$(basename "$FLOW_DIR")
# The version the plugin reports comes from the flow's own `version` field, so
# read it from there rather than keeping a second copy in this script.
VERSION=$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$FLOW_DIR/flow.toml" | head -1)
[ -n "$VERSION" ] || { echo "no version in $FLOW_DIR/flow.toml" >&2; exit 1; }

mkdir -p "$OUT_DIR"
ARCHIVE="$OUT_DIR/$NAME-$VERSION.tar.gz"

# Only the runtime files go on the device; README.md is for readers of the repo.
# COPYFILE_DISABLE keeps macOS from adding ._ AppleDouble entries, which would
# land in the flows directory on the device.
COPYFILE_DISABLE=1 tar czf "$ARCHIVE" -C "$FLOW_DIR" --exclude=README.md .

echo "$ARCHIVE"
echo
echo "Upload to Cumulocity > Management > Software repository:"
echo "  Name       c8y/$NAME"
echo "  Version    $VERSION"
echo "  Type       flow"
echo "  File       $ARCHIVE"
echo
echo "Contents:"
tar tzf "$ARCHIVE" | sed 's/^/  /'
