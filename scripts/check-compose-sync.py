#!/usr/bin/env python3
"""
Assert docker-compose.yml and docker-compose_vision_demo.yml stay in step.

Those two files describe the same deployment for different targets: the first
runs on a workstation, the second is uploaded to Cumulocity as a
`container-group` artifact and materialized on the device. Their bind mounts
must therefore match, but nothing enforces it -- and the duplication is not
removable, because the deployment copy needs absolute paths, `version: "3.7"`,
and no Compose-spec keys for the device's docker-compose 1.29.2.

Drift here fails in a way that is easy to miss: a deployment copy missing the
entrypoint.sh mount starts a container with no capture process, and the
preprocessor -- which only reads frames off disk -- then fails every cycle once
the leftover frames pass frame_max_age_s. The first ~2 minutes look healthy.

Exits non-zero and prints what differs. Deliberately dependency-free (no PyYAML)
so it runs anywhere python3 does.
"""

import re
import sys
from pathlib import Path

DEV = "docker-compose.yml"
DEPLOY = "docker-compose_vision_demo.yml"

# `- <source>:<target>` or `- <source>:<target>:ro`, ignoring commented-out lines.
VOLUME_RE = re.compile(r"^\s*-\s+(?P<src>[^:\s#]+):(?P<dst>/[^:\s]+)(?::(?P<opts>[a-z,]+))?\s*$")


def parse(path):
    """Container-side mount targets in order, plus network_mode."""
    mounts, network_mode = [], None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = VOLUME_RE.match(line)
        if m:
            mounts.append(m.group("dst"))
        elif "network_mode:" in line:
            network_mode = line.split("network_mode:", 1)[1].strip()
    return mounts, network_mode


def main():
    root = Path(__file__).resolve().parent.parent
    for f in (DEV, DEPLOY):
        if not (root / f).exists():
            print(f"error: {f} not found", file=sys.stderr)
            return 2

    dev_mounts, dev_net = parse(root / DEV)
    dep_mounts, dep_net = parse(root / DEPLOY)

    problems = []

    if not dev_mounts:
        problems.append(f"{DEV}: parsed zero mounts -- the check itself is broken")

    if dev_mounts != dep_mounts:
        only_dev = [m for m in dev_mounts if m not in dep_mounts]
        only_dep = [m for m in dep_mounts if m not in dev_mounts]
        if only_dev:
            problems.append(f"missing from {DEPLOY}: " + ", ".join(only_dev))
        if only_dep:
            problems.append(f"missing from {DEV}: " + ", ".join(only_dep))
        if not only_dev and not only_dep:
            problems.append(
                f"same mounts in a different order:\n"
                f"  {DEV}:    {dev_mounts}\n"
                f"  {DEPLOY}: {dep_mounts}"
            )

    if dev_net != dep_net:
        problems.append(f"network_mode differs: {DEV}={dev_net!r}, {DEPLOY}={dep_net!r}")

    if problems:
        print("compose files out of sync:\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            f"\nBoth files must mount the same container-side paths in the same order.\n"
            f"Host paths differ on purpose ({DEPLOY} uses absolute /opt/vision_demo/...\n"
            f"because tedge-container-plugin runs it from its own directory).",
            file=sys.stderr,
        )
        return 1

    print(f"compose files in sync -- {len(dev_mounts)} mounts, network_mode={dev_net}:")
    for m in dev_mounts:
        print(f"  {m}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
