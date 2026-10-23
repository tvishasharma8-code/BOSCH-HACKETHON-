"""Shared contract for every telemetry tool. All tools are READ-ONLY.

Every tool function returns the dict built by `tool_result(...)`, so the agent
loop can treat all tools the same way and cite evidence exactly.
"""
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
JAEGER_URL = os.getenv("JAEGER_URL", "http://localhost:8080/jaeger/ui")
HTTP_TIMEOUT_S = 5

# Diagnosis must come from telemetry only. Never query or read these.
EXCLUDED_SERVICES = {"flagd", "flagd-ui"}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def evidence(source, query, value, **extra):
    """One checkable fact: where it came from, the exact query, and the value."""
    return {"source": source, "query": query, "value": value, "timestamp": now_iso(), **extra}


def tool_result(tool, summary, evidence_list=None, error=None):
    return {
        "tool": tool,
        "summary": summary,
        "evidence": evidence_list or [],
        "error": error,
    }
