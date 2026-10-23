"""Trace tools (owner: A). READ-ONLY queries against Jaeger's v3 API (OTLP JSON).

Key idea: the first service showing errors is often a victim. `get_trace` walks
the span tree and points at the DEEPEST failing span, which is usually the cause.
"""
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx

from agent.tools.common import (
    EXCLUDED_SERVICES, HTTP_TIMEOUT_S, JAEGER_URL,
    ToolInputError, check_service, key_fact, tool_result,
)

KIND = {1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer"}
STATUS_ERROR = 2
TRACE_ID_RE = re.compile(r"^[0-9a-f]{16,32}$")
# Flag data must never reach the LLM (R4): drop these span attributes.
REDACTED_ATTR_PREFIXES = ("feature_flag", "openfeature")
# Span attributes worth showing; everything else is dropped to keep output small.
USEFUL_ATTRS = ("http.response.status_code", "http.status_code", "rpc.grpc.status_code", "rpc.method",
                "db.system", "db.operation", "server.address", "peer.service", "net.peer.name",
                "app.product.id", "error.type", "exception.type", "exception.message")


def _get(path, params=None):
    for attempt in range(2):  # one retry
        try:
            resp = httpx.get(f"{JAEGER_URL}{path}", params=params, timeout=HTTP_TIMEOUT_S)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError:
            if attempt == 1:
                raise


def _value(v):
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            return v[k]
    return None


def _attrs(items):
    out = {}
    for a in items or []:
        key = a["key"]
        if key.startswith(REDACTED_ATTR_PREFIXES):
            continue
        if key in USEFUL_ATTRS:
            out[key] = _value(a["value"])
    return out


def _flatten(resource_spans):
    """OTLP JSON -> flat list of simplified spans, excluded services dropped."""
    spans = []
    for rs in resource_spans:
        service = next((_value(a["value"]) for a in rs["resource"]["attributes"]
                        if a["key"] == "service.name"), "unknown")
        if service in EXCLUDED_SERVICES:
            continue
        for ss in rs.get("scopeSpans", []):
            for s in ss.get("spans", []):
                status = s.get("status") or {}
                event_attrs = [a for e in s.get("events", []) for a in e.get("attributes", [])]
                spans.append({
                    "trace_id": s["traceId"],
                    "span_id": s["spanId"],
                    "parent_id": s.get("parentSpanId") or None,
                    "service": service,
                    "operation": s["name"],
                    "kind": KIND.get(s.get("kind"), "unknown"),
                    "duration_ms": round((int(s["endTimeUnixNano"]) - int(s["startTimeUnixNano"])) / 1e6, 2),
                    "start_ns": int(s["startTimeUnixNano"]),
                    "error": status.get("code") == STATUS_ERROR,
                    # First line only: stack traces make the output unreadable.
                    "status_message": (status.get("message") or "").strip().splitlines()[0][:300]
                                      if status.get("message") else "",
                    "attributes": {**_attrs(s.get("attributes")), **_attrs(event_attrs)},
                })
    return spans


def _depth(span, by_id):
    depth, seen = 0, set()
    while span["parent_id"] in by_id and span["span_id"] not in seen:
        seen.add(span["span_id"])
        span = by_id[span["parent_id"]]
        depth += 1
    return depth


def _deepest_error(spans, service=None):
    """Deepest error span. With `service`, only look at that service's error spans and
    errors below them, so unrelated errors elsewhere in a big trace are ignored."""
    by_id = {s["span_id"]: s for s in spans}
    errors = [s for s in spans if s["error"]]
    if service:
        roots = {s["span_id"] for s in errors if s["service"] == service}

        def under_root(s):
            seen = set()
            while s and s["span_id"] not in seen:
                if s["span_id"] in roots:
                    return True
                seen.add(s["span_id"])
                s = by_id.get(s["parent_id"])
            return False

        errors = [s for s in errors if under_root(s)]
    return max(errors, key=lambda s: _depth(s, by_id)) if errors else None


def search_traces(service, errors_only=True, lookback_minutes=10, limit=20, min_duration_ms=None):
    """Recent traces through a service. errors_only keeps traces with an error span; min_duration_ms keeps slow ones."""
    try:
        service = check_service(service)
        lookback_minutes, limit = int(lookback_minutes), int(limit)
        if not 1 <= lookback_minutes <= 60 or not 1 <= limit <= 50:
            raise ToolInputError("lookback_minutes must be 1..60 and limit 1..50")
    except (ToolInputError, ValueError) as exc:
        return tool_result("search_traces", {"service": service}, "invalid input", error=str(exc))

    end = datetime.now(timezone.utc)
    params = {
        "query.service_name": service,
        "query.start_time_min": (end - timedelta(minutes=lookback_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "query.start_time_max": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "query.num_traces": 100 if errors_only else limit,
    }
    if min_duration_ms:
        params["query.duration_min"] = f"{int(min_duration_ms)}ms"
    query = {"service": service, "errors_only": errors_only, "lookback_minutes": lookback_minutes,
             "limit": limit, "min_duration_ms": min_duration_ms}
    try:
        data = _get("/api/v3/traces", params)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:  # Jaeger answers 404 when nothing matches
            return tool_result("search_traces", query, f"no traces for {service} in the last {lookback_minutes}m", raw=[])
        return tool_result("search_traces", query, "Jaeger unavailable", error=str(exc))
    except httpx.HTTPError as exc:
        return tool_result("search_traces", query, "Jaeger unavailable", error=str(exc))

    traces = {}
    for span in _flatten(data.get("result", {}).get("resourceSpans", [])):
        traces.setdefault(span["trace_id"], []).append(span)

    rows = []
    for trace_id, spans in traces.items():
        deepest = _deepest_error(spans, service)
        if errors_only and not deepest:
            continue
        ids = {s["span_id"] for s in spans}
        roots = [s for s in spans if s["parent_id"] not in ids] or spans
        root = max(roots, key=lambda s: s["duration_ms"])  # clocks differ per service, so don't trust start times
        rows.append({
            "trace_id": trace_id,
            "root": f"{root['service']} {root['operation']}",
            "duration_ms": max(s["duration_ms"] for s in spans),
            "span_count": len(spans),
            "error_spans": sum(s["error"] for s in spans),
            "deepest_error": f"{deepest['service']} {deepest['operation']}: {deepest['status_message']}" if deepest else None,
            "deepest_error_service": deepest["service"] if deepest else None,
        })
    rows = sorted(rows, key=lambda r: -r["duration_ms"])[:limit]

    kind = "error traces" if errors_only else "traces"
    lines = [f"{len(rows)} {kind} through {service} in the last {lookback_minutes}m."]
    culprits = Counter(r["deepest_error_service"] for r in rows if r["deepest_error_service"])
    if culprits:
        lines.append("Deepest failing service across these traces: "
                     + ", ".join(f"{svc} ({n}/{len(rows)})" for svc, n in culprits.most_common()))
    for r in rows[:10]:
        lines.append(f"- {r['trace_id']}  {r['duration_ms']:.0f}ms  {r['span_count']} spans  root: {r['root']}"
                     + (f"\n    deepest error: {r['deepest_error']}" if r["deepest_error"] else ""))
    facts = [key_fact("trace", service, **r) for r in rows]
    return tool_result("search_traces", query, "\n".join(lines), facts, raw=rows)


def get_trace(trace_id, service=None):
    """One trace as a span tree, with the deepest failing span marked (below `service`'s errors if given)."""
    trace_id = (trace_id or "").lower()
    if not TRACE_ID_RE.match(trace_id):
        return tool_result("get_trace", {"trace_id": trace_id}, "invalid input", error="trace_id must be hex")
    if service:
        try:
            service = check_service(service)
        except ToolInputError as exc:
            return tool_result("get_trace", {"trace_id": trace_id, "service": service}, "invalid input", error=str(exc))
    query = {"trace_id": trace_id, "service": service}
    try:
        data = _get(f"/api/v3/traces/{trace_id}")
    except httpx.HTTPError as exc:
        return tool_result("get_trace", query, "trace not found or Jaeger unavailable", error=str(exc))

    spans = _flatten(data.get("result", {}).get("resourceSpans", []))
    if not spans:
        return tool_result("get_trace", query, "trace has no spans", raw=[])
    by_id = {s["span_id"]: s for s in spans}
    children = {}
    for s in spans:
        children.setdefault(s["parent_id"] if s["parent_id"] in by_id else None, []).append(s)
    deepest = _deepest_error(spans, service)

    lines = [f"Trace {trace_id}: {len(spans)} spans, {sum(s['error'] for s in spans)} with errors."]

    def walk(parent, indent):
        for s in sorted(children.get(parent, []), key=lambda s: s["start_ns"]):
            # Keep the tree readable: show error spans, server/consumer spans and anything slow.
            if s["error"] or s["kind"] in ("server", "consumer") or s["duration_ms"] > 100 or indent < 2:
                mark = " <== DEEPEST ERROR" if deepest and s is deepest else (" [ERROR]" if s["error"] else "")
                msg = f" \"{s['status_message']}\"" if s["status_message"] else ""
                lines.append(f"{'  ' * indent}{s['service']} {s['operation']} ({s['kind']}, {s['duration_ms']:.0f}ms){mark}{msg}")
            walk(s["span_id"], indent + 1)

    if deepest:
        # Show only the path from the root down to the deepest error: that's the story.
        path, s = [], deepest
        while s and s not in path:
            path.append(s)
            s = by_id.get(s["parent_id"])
        for depth, s in enumerate(reversed(path)):
            mark = " <== DEEPEST ERROR" if s is deepest else (" [ERROR]" if s["error"] else "")
            msg = f" \"{s['status_message']}\"" if s["status_message"] else ""
            lines.append(f"{'  ' * depth}{s['service']} {s['operation']} ({s['kind']}, {s['duration_ms']:.0f}ms){mark}{msg}")
    else:
        walk(None, 0)
    if deepest:
        lines.append(f"Deepest failing span: {deepest['service']} / {deepest['operation']}"
                     + (f" - {deepest['status_message']}" if deepest["status_message"] else ""))

    facts = [key_fact("trace", s["service"], trace_id=trace_id, span_id=s["span_id"], operation=s["operation"],
                      kind=s["kind"], duration_ms=s["duration_ms"], status="ERROR" if s["error"] else "OK",
                      message=s["status_message"], deepest_error=s is deepest, attributes=s["attributes"])
             for s in spans if s["error"] or s["kind"] in ("server", "consumer")]
    return tool_result("get_trace", query, "\n".join(lines[:60]), facts, raw=spans)


def list_services():
    """Services that have sent traces (flag services excluded)."""
    try:
        services = _get("/api/v3/services").get("services", [])
    except httpx.HTTPError as exc:
        return tool_result("list_services", {}, "Jaeger unavailable", error=str(exc))
    services = sorted(s for s in services if s not in EXCLUDED_SERVICES)
    return tool_result("list_services", {}, f"{len(services)} services: " + ", ".join(services),
                       [key_fact("topology", None, services=services)], raw=services)
