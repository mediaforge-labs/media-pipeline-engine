#!/usr/bin/env python3
from __future__ import annotations

"""Production entrypoint with the authoritative Dell repository contract gate."""

import re
import sys

import factory_worker_v4 as production


_original_transient_failure = production.base.is_transient_failure


def _is_transient_failure_v7(error: str) -> bool:
    """Treat Dell transport/gateway outages as recoverable production failures."""
    text = (error or "").lower()
    dell_patterns = (
        r"dell_asset_client_v[5-9]\.py.*returned non-zero exit status",
        r"dell asset gateway.*offline",
        r"dell asset gateway.*did not become healthy",
        r"dell public gateway health check failed",
        r"failed to resolve.*trycloudflare\.com",
        r"nameresolutionerror",
        r"gateway heartbeat is stale",
        r"waiting for the workstation gateway",
    )
    if any(re.search(pattern, text) for pattern in dell_patterns):
        return True
    return _original_transient_failure(error)


# mark_failed() lives in factory_worker_v2 and resolves this function dynamically.
# Patching the module-level classifier here keeps transport outages pending/retryable
# without weakening deterministic render/content validation failures.
production.base.is_transient_failure = _is_transient_failure_v7


def main() -> None:
    if "--asset-client" not in sys.argv:
        sys.argv.extend(["--asset-client", "public-worker-template/dell_asset_client_v7.py"])
    production.impl.main()


if __name__ == "__main__":
    main()
