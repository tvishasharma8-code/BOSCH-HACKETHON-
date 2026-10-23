"""Shared helpers for every telemetry tool. All tools are READ-ONLY.

Every tool returns the dict built by `tool_result(...)`. Its fields match the
`ToolResult` model in the plan (Section 6.2); the evidence store (D) adds
`evidence_id` and `raw_ref` when it records the result.
"""
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
CONFIG = yaml.safe_load(CONFIG_PATH.read_text())

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", CONFIG["endpoints"]["prometheus"])
JAEGER_URL = os.getenv("JAEGER_URL", CONFIG["endpoints"]["jaeger"])
HTTP_TIMEOUT_S = CONFIG.get("metrics", {}).get("http_timeout_s", 5)

# Diagnosis must come from telemetry only (R4). The names live in config, not in agent code.
EXCLUDED_SERVICES = set(CONFIG.get("excluded_services", []))

# Service names are interpolated into queries, so only allow plain names.
SERVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
# Windows like 30s, 5m, 1h.
WINDOW_RE = re.compile(r"^(\d{1,4})([smh])$")


class ToolInputError(ValueError):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def check_service(service):
    if not SERVICE_NAME_RE.match(service or ""):
        raise ToolInputError(f"invalid service name: {service!r}")
    if service in EXCLUDED_SERVICES:
        raise ToolInputError(f"service {service!r} is excluded from agent tools")
    return service


def check_window(window, max_minutes):
    match = WINDOW_RE.match(window or "")
    if not match:
        raise ToolInputError(f"invalid window: {window!r} (use e.g. 30s, 5m, 1h)")
    value, unit = int(match.group(1)), match.group(2)
    minutes = value / 60 if unit == "s" else value * 60 if unit == "h" else value
    if minutes > max_minutes:
        raise ToolInputError(f"window {window} exceeds the {max_minutes}m limit")
    return window


def key_fact(fact_type, service, **data):
    """One machine-checkable atom the diagnosis can cite."""
    return {"type": fact_type, "service": service, "data": data}


def tool_result(tool, query, summary, key_facts=None, raw=None, error=None):
    return {
        "tool": tool,
        "query": query,
        "ts": now_iso(),
        "summary": summary,
        "key_facts": key_facts or [],
        "raw": raw,
        "error": error,
    }
