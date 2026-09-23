#!/usr/bin/env python3
from __future__ import annotations

"""MediaForge Dell gateway v3.2 compatibility layer.

This keeps the proven v3 catalog/auth logic but upgrades the public transport contract:
- authoritative version expected by the GitHub production client;
- HTTP byte-range support so availability probes transfer only the requested byte;
- graceful client disconnect handling while Cloudflare/GitHub probes assets.
"""

import importlib.util
import mimetypes
import pathlib
import re
import urllib.parse
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
LEGACY = HERE / "dell_asset_gateway.py"

spec = importlib.util.spec_from_file_location("mediaforge_dell_gateway_legacy", LEGACY)
if spec is None or spec.loader is None:
    raise SystemExit("Unable to load legacy Dell asset gateway")
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)

GATEWAY_VERSION = "3.2-active-gta-vi-rest"
legacy.GATEWAY_VERSION = GATEWAY_VERSION


def _parse_range(value: str, size: int) -> tuple[int, int] | None:
    if not value:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match:
        raise ValueError("unsupported_range")
    start_raw, end_raw = match.groups()
    if not start_raw and not end_raw:
        raise ValueError("empty_range")

    if start_raw:
        start = int(start_raw)
        if start >= size:
            raise ValueError("range_start_out_of_bounds")
        end = int(end_raw) if end_raw else size - 1
        end = min(end, size - 1)
        if end < start:
            raise ValueError("range_end_before_start")
    else:
        suffix = int(end_raw)
        if suffix <= 0:
            raise ValueError("invalid_suffix_range")
        suffix = min(suffix, size)
        start = size - suffix
        end = size - 1
    return start, end


class HandlerV32(legacy.Handler):
    server_version = "MediaForgeDellGateway/3.2"

    def _asset_error(self, status: int, payload: dict):
        return self._json(status, payload)

    def do_GET(self):
        state = self.server.state

        if self.path == "/health":
            return self._json(
                200,
                {
                    "ok": True,
                    "gateway_id": state.args.gateway_id,
                    "version": GATEWAY_VERSION,
                    "catalog_assets": len(state.assets),
                    "active_collection": "media/gta_vi",
                    "eligible_suffix": legacy.ACTIVE_EDIT_SUFFIX,
                    "catalog_generated_at": state.catalog_meta.get("generated_at"),
                    "range_requests": True,
                },
            )

        parsed_path = urllib.parse.urlparse(self.path).path
        match = re.fullmatch(r"/asset/([0-9a-f]{32})/([a-f0-9]{24})", parsed_path)
        if not match:
            return self._asset_error(404, {"error": "not_found"})

        request_id, asset_id = match.groups()
        token = self.headers.get("X-MediaForge-Request-Token", "")
        if not token:
            return self._asset_error(401, {"error": "missing_token"})

        reqrow = legacy.get_json(state.s3, f"gateway/requests/{request_id}.json")
        if (
            not reqrow
            or reqrow.get("request_token") != token
            or reqrow.get("gateway_id") != state.args.gateway_id
        ):
            return self._asset_error(403, {"error": "invalid_request"})

        try:
            expires = datetime.fromisoformat(
                str(reqrow.get("expires_at") or "").replace("Z", "+00:00")
            )
        except Exception:
            return self._asset_error(403, {"error": "bad_expiry"})

        if expires <= datetime.now(timezone.utc):
            return self._asset_error(403, {"error": "expired"})
        if asset_id not in (reqrow.get("asset_ids") or []):
            return self._asset_error(403, {"error": "asset_not_allowed"})

        item = state.assets.get(asset_id)
        if not item:
            return self._asset_error(404, {"error": "asset_missing"})

        root = pathlib.Path(state.args.root).resolve()
        path = (root / item["relative_path"]).resolve()
        try:
            path.relative_to(root / "media" / "gta_vi")
        except Exception:
            return self._asset_error(403, {"error": "unsafe_or_inactive_path"})

        if not path.is_file() or not path.name.lower().endswith(legacy.ACTIVE_EDIT_SUFFIX):
            return self._asset_error(404, {"error": "file_offline"})

        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        range_header = self.headers.get("Range", "")
        selected = None
        if range_header:
            try:
                selected = _parse_range(range_header, size)
            except (TypeError, ValueError):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        if selected is None:
            start, end = 0, size - 1
            status = 200
        else:
            start, end = selected
            status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()

        try:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(8 * 1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # Availability probes intentionally read a tiny range and disconnect quickly.
            return


def main():
    legacy.Handler = HandlerV32
    legacy.GATEWAY_VERSION = GATEWAY_VERSION
    legacy.main()


if __name__ == "__main__":
    main()
