#!/usr/bin/env python3
"""Local readiness check; unlike liveness it requires recovery to be complete."""

from healthcheck import check


if __name__ == "__main__":
    ok, reason = check(require_ready=True)
    if not ok:
        print(reason)
        raise SystemExit(1)
