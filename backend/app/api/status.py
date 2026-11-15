"""
Infrastructure health and telemetry endpoint (/api/status).
"""

import ipaddress
import os
import shutil
import socket
import time
import urllib.parse
from typing import Any

import httpx
from flask import Blueprint, current_app, jsonify

from .. import config

status_bp = Blueprint("status", __name__)

BLOCKED_METADATA_HOSTNAMES = {
    "metadata.google.internal",
    "metadata",
    "instance-data",
    "instance-data.ec2.internal",
}

TRUSTED_LLM_DOMAINS = {
    "openai.com",
    "anthropic.com",
    "groq.com",
    "mistral.ai",
    "deepseek.com",
    "openrouter.ai",
    "together.xyz",
    "cohere.ai",
    "cohere.com",
}


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if IP address is a blocked destination (link-local/metadata, multicast, etc.)."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return False
    return (
        ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


def _validate_probe_url(base_url: str) -> tuple[bool, str, str]:
    """Validate probe URL against SSRF and extract hostname.

    Returns (is_safe, hostname, error_message).
    """
    if not base_url or not isinstance(base_url, str):
        return False, "", "Disallowed endpoint: URL is empty or invalid (SSRF protection)"

    parsed = urllib.parse.urlsplit(base_url.strip())
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return False, "", f"Disallowed URL scheme '{scheme}': only http and https are permitted (SSRF protection)"

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False, "", "Disallowed endpoint: missing host in URL (SSRF protection)"

    if hostname in BLOCKED_METADATA_HOSTNAMES:
        return False, hostname, f"Blocked metadata host '{hostname}' (SSRF protection)"

    is_ip_literal = False
    for parse_fn in (
        lambda h: ipaddress.ip_address(h),
        lambda h: ipaddress.IPv4Address(socket.inet_aton(h)),
    ):
        try:
            ip = parse_fn(hostname)
            is_ip_literal = True
            if _is_blocked_ip(ip):
                return False, hostname, f"Blocked destination IP {hostname} (SSRF protection)"
        except (ValueError, OSError):
            continue

    if not is_ip_literal and hostname != "localhost":
        try:
            addr_infos = socket.getaddrinfo(hostname, None)
            for _, _, _, _, sockaddr in addr_infos:
                try:
                    resolved_ip = ipaddress.ip_address(sockaddr[0])
                    if _is_blocked_ip(resolved_ip):
                        return False, hostname, f"Blocked destination host resolving to {sockaddr[0]} (SSRF protection)"
                except ValueError:
                    continue
        except socket.gaierror:
            pass

    return True, hostname, ""


def _is_trusted_llm_host(hostname: str) -> bool:
    """Check if host is safe to receive Authorization Bearer credentials."""
    if not hostname:
        return False
    if hostname == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_loopback:
            return True
    except ValueError:
        pass

    for domain in TRUSTED_LLM_DOMAINS:
        if hostname == domain or hostname.endswith("." + domain):
            return True
    return False


def probe_llm_endpoint(timeout_sec: float = 3.0) -> dict[str, Any]:
    """Probe configured LLM endpoint without making expensive generation calls."""
    base_url = config.Config.get_llm_base_url()
    start = time.perf_counter()

    is_safe, hostname, err_msg = _validate_probe_url(base_url)
    safe_endpoint = str(base_url or "")
    api_key = config.Config.get_llm_api_key()
    if api_key and api_key in safe_endpoint:
        safe_endpoint = safe_endpoint.replace(api_key, "[REDACTED]")

    if not is_safe:
        return {
            "status": "unavailable",
            "endpoint": safe_endpoint,
            "error": err_msg,
            "latency_ms": 0.0,
        }

    headers = {}
    if api_key and _is_trusted_llm_host(hostname):
        headers["Authorization"] = f"Bearer {api_key}"

    # Try models endpoint
    probe_url = base_url.rstrip("/") + "/models"
    try:
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.get(probe_url, headers=headers)
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            if resp.status_code in {200, 401, 403}:
                # Endpoint reachable (even if auth required on models list)
                return {
                    "status": "ok" if resp.status_code == 200 else "degraded",
                    "endpoint": safe_endpoint,
                    "latency_ms": latency_ms,
                    "model": config.Config.LLM_MODEL_NAME,
                }
            return {
                "status": "degraded",
                "endpoint": safe_endpoint,
                "latency_ms": latency_ms,
                "http_status": resp.status_code,
            }
    except Exception as e:  # noqa: BLE001
        err = str(e)
        if api_key and api_key in err:
            err = err.replace(api_key, "[REDACTED]")
        return {
            "status": "unavailable",
            "endpoint": safe_endpoint,
            "error": err,
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
        }


def check_disk_usage() -> dict[str, Any]:
    """Check storage capacity in the upload and simulation directory."""
    folder = config.Config.UPLOAD_FOLDER
    os.makedirs(folder, exist_ok=True)
    try:
        total, used, free = shutil.disk_usage(folder)
        free_gb = round(free / (1024 ** 3), 2)
        total_gb = round(total / (1024 ** 3), 2)
        used_percent = round((used / total) * 100, 1)

        status = "ok"
        if free_gb < 1.0 or used_percent > 95.0:
            status = "degraded"

        return {
            "status": status,
            "free_gb": free_gb,
            "total_gb": total_gb,
            "used_percent": used_percent,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "status": "unavailable",
            "error": str(e),
        }


@status_bp.route("/status", methods=["GET"])
def get_infrastructure_status():
    """
    GET /api/status
    Returns health and telemetry diagnostics across Neo4j, LLM, and disk storage.
    """
    storage = current_app.extensions.get("neo4j_storage")
    if storage is not None and hasattr(storage, "check_health"):
        neo4j_status = storage.check_health()
    else:
        neo4j_status = {
            "status": "unavailable",
            "error": "Neo4jStorage instance not initialized",
        }

    llm_status = probe_llm_endpoint()
    disk_status = check_disk_usage()

    overall_status = "ok"
    if neo4j_status.get("status") == "unavailable":
        overall_status = "unavailable"
    elif any(
        c.get("status") in {"degraded", "unavailable"}
        for c in (neo4j_status, llm_status, disk_status)
    ):
        overall_status = "degraded"

    http_code = 503 if overall_status == "unavailable" else 200

    response_payload = {
        "status": overall_status,
        "components": {
            "neo4j": neo4j_status,
            "llm": llm_status,
            "disk": disk_status,
        },
    }
    return jsonify(response_payload), http_code
