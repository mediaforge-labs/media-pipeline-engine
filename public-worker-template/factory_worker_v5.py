#!/usr/bin/env python3
from __future__ import annotations

"""Production entrypoint with the authoritative Dell repository contract gate."""

import sys

import factory_worker_v4 as production


def main() -> None:
    if "--asset-client" not in sys.argv:
        sys.argv.extend(["--asset-client", "public-worker-template/dell_asset_client_v6.py"])
    production.impl.main()


if __name__ == "__main__":
    main()
