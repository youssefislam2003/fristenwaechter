#!/usr/bin/env python3
"""Container healthcheck for the worker (dead-man switch).

Exits 0 while the worker's liveness file is FRESH (touched within the allowed
window by the last healthy relay cycle), non-zero once it goes stale — so
Docker/Kubernetes restarts a wedged worker even if it never crashed outright.

The relay runs every 15 s and touches the file after each successful cycle, so
a 90 s staleness threshold tolerates a couple of missed cycles before firing.
"""
from __future__ import annotations

import os
import sys
import time

STALE_SECONDS = 90


def main() -> int:
    path = os.environ.get("WORKER_HEARTBEAT_FILE", "/tmp/fristen-worker.alive")
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        print(f"heartbeat file missing: {path}", file=sys.stderr)
        return 1
    if age > STALE_SECONDS:
        print(f"heartbeat stale: {age:.0f}s > {STALE_SECONDS}s", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
