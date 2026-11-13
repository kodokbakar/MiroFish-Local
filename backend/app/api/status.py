"""
Infrastructure health and telemetry endpoint (/api/status).
"""

import os
import shutil
import time
from typing import Dict, Any
from flask import Blueprint, jsonify, current_app
import httpx

from ..config import Config

status_bp = Blueprint("status", __name__)


def probe_llm_endpoint(timeout_sec: float = 3.0) -> Dict[str, Any]:
    """Probe configured LLM endpoint without making expensive generation calls."""
    base_url = Config.get_llm_base_url()
    start = time.perf_counter()
    headers = {}
    api_key = Config.get_llm_api_key()
    if api_key:
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
                    "endpoint": base_url,
                    "latency_ms": latency_ms,
                    "model": Config.LLM_MODEL_NAME,
                }
            return {
                "status": "degraded",
                "endpoint": base_url,
                "latency_ms": latency_ms,
                "http_status": resp.status_code,
            }
    except Exception as e:
        return {
            "status": "unavailable",
            "endpoint": base_url,
            "error": str(e),
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
        }


def check_disk_usage() -> Dict[str, Any]:
    """Check storage capacity in the upload and simulation directory."""
    folder = Config.UPLOAD_FOLDER
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
    except Exception as e:
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
