#!/usr/bin/env python3
from __future__ import annotations

"""Harden Dell asset authorization without changing semantic selection logic.

V5 keeps semantic selection from the proven legacy client while hardening transport:
- one short-lived authorization per selected asset;
- oversized legacy catalog authorization is bypassed;
- stale->stale->valid replacement chains keep their semantic mapping correct;
- a live /health response is authoritative, so an old stored heartbeat cannot block a
  healthy persistent Dell gateway.
"""

import importlib.util
import inspect
import pathlib
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
LEGACY = HERE / "dell_asset_client.py"

spec = importlib.util.spec_from_file_location("mediaforge_dell_asset_client_legacy", LEGACY)
if spec is None or spec.loader is None:
    raise SystemExit("Unable to load Dell asset client")
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)


def _patch_legacy_multihop_mapping() -> None:
    """Fix stale->stale->valid replacement chains inside legacy.main()."""
    source = inspect.getsource(legacy.main)
    needle = "update_selection_for_replacement(original, replacement, reason)"
    fixed = "update_selection_for_replacement(item, replacement, reason)"
    if needle not in source:
        raise SystemExit("Dell legacy fallback mapping patch no longer matches source; aborting safely")
    exec(source.replace(needle, fixed, 1), legacy.__dict__)


_patch_legacy_multihop_mapping()
_original_control = legacy.control


def _confirm_live_gateway(status: dict) -> dict:
    public_url = str(status.get("public_url") or "").rstrip("/")
    if not public_url:
        return status
    try:
        response = legacy.requests.get(
            f"{public_url}/health",
            headers={"User-Agent": "MediaForge-GitHub/5.3-live-health"},
            timeout=12,
        )
        if response.status_code != 200:
            return status
        health = response.json()
        if not health.get("ok") or str(health.get("gateway_id") or "") != "dell-main":
            return status
        live = dict(status)
        live["status"] = "online"
        live["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        live["gateway_live_health"] = True
        live["gateway_version"] = health.get("version") or live.get("gateway_version")
        live["catalog_assets"] = health.get("catalog_assets") or live.get("catalog_assets")
        return live
    except Exception:
        return status


def _control_v5(action: str, body: dict | None = None):
    if action == "create_request" and body is not None:
        asset_ids = list(body.get("asset_ids") or [])
        if len(asset_ids) > 200:
            print(
                {
                    "legacy_catalog_authorization_bypassed": True,
                    "supplied": len(asset_ids),
                    "gateway_max": 200,
                    "reason": "v5 uses single-asset authorization",
                }
            )
            return {
                "status": "bypassed",
                "request_id": str(body.get("request_id") or ""),
                "asset_count": len(asset_ids),
            }
    result = _original_control(action, body)
    if action == "status" and isinstance(result, dict):
        return _confirm_live_gateway(result)
    return result


legacy.control = _control_v5


def _delete_request(request_id: str) -> None:
    try:
        legacy.control("delete_request", {"request_id": request_id})
    except Exception:
        pass


def _create_single_asset_request(asset_id: str) -> tuple[str, str]:
    request_id = uuid.uuid4().hex
    token = uuid.uuid4().hex + uuid.uuid4().hex
    expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    legacy.control(
        "create_request",
        {
            "request_id": request_id,
            "request_token": token,
            "job_key": f"single-asset-{asset_id}-{request_id[:8]}",
            "asset_ids": [asset_id],
            "expires_at": expires,
        },
    )
    return request_id, token


def download_asset(status: dict, ignored_request_id: str, ignored_token: str, item: dict, out: pathlib.Path):
    """Download with one authorization per asset.

    - 403: recreate authorization and retry the SAME asset; never rotate media.
    - 404/410: stale/missing catalog entry; caller may choose a replacement.
    - transient/network errors: refresh tunnel and retry.
    Persistent 403 aborts immediately because replacing media cannot fix auth.
    """
    del ignored_request_id, ignored_token
    asset_id = str(item["asset_id"])
    current_status = status
    last_error = "unknown"
    transient_codes = {408, 429, 500, 502, 503, 504}

    for auth_attempt in range(1, 4):
        request_id = ""
        token = ""
        part = out.with_suffix(out.suffix + ".part")
        part.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
        try:
            request_id, token = _create_single_asset_request(asset_id)
            public_url = str(current_status.get("public_url") or "").rstrip("/")
            if not public_url:
                raise RuntimeError("Dell asset gateway public_url is empty")
            url = f"{public_url}/asset/{request_id}/{asset_id}"
            request = urllib.request.Request(
                url,
                headers={
                    "X-MediaForge-Request-Token": token,
                    "User-Agent": "MediaForge-GitHub/5.3-live-health",
                },
            )
            with urllib.request.urlopen(request, timeout=1800) as source, part.open("wb") as target:
                while True:
                    chunk = source.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)

            expected = legacy.asset_size(item)
            actual = part.stat().st_size
            if expected > 0 and actual != expected:
                raise RuntimeError(f"size mismatch: expected={expected} actual={actual}")
            if actual <= 0:
                raise RuntimeError("downloaded file is empty")
            part.replace(out)
            return True, "ok", current_status

        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            part.unlink(missing_ok=True)
            if exc.code in {404, 410}:
                return False, last_error, current_status
            if exc.code == 403:
                if auth_attempt >= 3:
                    raise SystemExit(
                        f"Dell gateway authorization failed three times for asset {asset_id} (HTTP 403). "
                        "Aborting before TTS/render because replacing the video cannot fix gateway auth."
                    )
            elif exc.code not in transient_codes:
                raise SystemExit(f"Dell gateway returned non-recoverable {last_error} for asset {asset_id}")

        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, RuntimeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            part.unlink(missing_ok=True)
            if auth_attempt >= 3:
                return False, last_error, current_status

        finally:
            if request_id:
                _delete_request(request_id)

        try:
            refreshed = legacy.control("status")
            if refreshed.get("status") == "online" and refreshed.get("public_url"):
                current_status = refreshed
        except Exception:
            pass
        time.sleep(auth_attempt)

    return False, last_error, current_status


legacy.download_asset = download_asset
legacy.main()
