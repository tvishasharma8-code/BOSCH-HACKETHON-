"""Metrics tools (owner: A). READ-ONLY queries against Prometheus.

The LLM never writes PromQL. It picks a tool and a template; we build the query.

Flagging is dynamic: each service is compared to its own normal (a baseline
window that ends a few minutes ago), and flagged when it's `worse_factor` times
worse AND above a small fixed floor.
"""
import time

import httpx

from agent.tools.common import (
    CONFIG, EXCLUDED_SERVICES, HTTP_TIMEOUT_S, PROMETHEUS_URL,
    ToolInputError, check_service, check_window, key_fact, tool_result,
)

CFG = CONFIG["metrics"]

# A service's own traffic = requests it receives. Client spans are left out so a
# caller isn't blamed for its callee's errors (checkout calling a failing payment).
OWN_SPANS = 'span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"'
CALLS = "traces_span_metrics_calls_total"
DURATION = "traces_span_metrics_duration_milliseconds_bucket"
ERROR = 'status_code="STATUS_CODE_ERROR"'


# --- Prometheus access -------------------------------------------------------

def _get(path, params):
    for attempt in range(2):  # one retry
        try:
            resp = httpx.get(f"{PROMETHEUS_URL}{path}", params=params, timeout=HTTP_TIMEOUT_S)
            resp.raise_for_status()
            body = resp.json()
            if body.get("status") != "success":
                raise RuntimeError(body.get("error", "prometheus query failed"))
            return body["data"]
        except (httpx.HTTPError, RuntimeError):
            if attempt == 1:
                raise


def prom_query(promql):
    """Instant query -> list of Prometheus result series."""
    return _get("/api/v1/query", {"query": promql})["result"]


def prom_query_range(promql, minutes, step_s=None):
    end = time.time()
    step = step_s or CFG["range_step_s"]
    return _get("/api/v1/query_range",
                {"query": promql, "start": end - minutes * 60, "end": end, "step": step})["result"]


def _by(results, label):
    """Turn a Prometheus vector into {label_value: float}, dropping NaN and excluded services."""
    out = {}
    for r in results:
        name = r["metric"].get(label)
        value = float(r["value"][1])
        if name and name not in EXCLUDED_SERVICES and value == value:  # value == value drops NaN
            out[name] = value
    return out


# --- Query templates ---------------------------------------------------------

def _rate(selector, window, offset=""):
    return f"rate({selector}[{window}]{offset})"


def _queries(window, offset=""):
    """PromQL for every service at once, for `window` (optionally shifted back by `offset`)."""
    total = f"sum by (service_name) ({_rate(f'{CALLS}{{{OWN_SPANS}}}', window, offset)})"
    errors = f"sum by (service_name) ({_rate(f'{CALLS}{{{OWN_SPANS},{ERROR}}}', window, offset)})"

    def quantile(q):
        return (f"histogram_quantile({q}, sum by (service_name, le) "
                f"({_rate(f'{DURATION}{{{OWN_SPANS}}}', window, offset)}))")

    return {
        "request_rate": total,
        "error_ratio": f"({errors}) / ({total})",
        "p50_ms": quantile(0.50),
        "p95_ms": quantile(0.95),
        "p99_ms": quantile(0.99),
    }


def _service_templates(service, window):
    s = f'service_name="{service}",{OWN_SPANS}'
    c = f'container_name="{service}"'
    total = f"sum({_rate(f'{CALLS}{{{s}}}', window)})"
    errors = f"sum({_rate(f'{CALLS}{{{s},{ERROR}}}', window)})"

    def quantile(q):
        return f"histogram_quantile({q}, sum by (le) ({_rate(f'{DURATION}{{{s}}}', window)}))"

    return {
        "request_rate": total,
        "error_ratio": f"({errors}) / ({total})",
        "latency_p50_ms": quantile(0.50),
        "latency_p95_ms": quantile(0.95),
        "latency_p99_ms": quantile(0.99),
        # Which operation inside the service is failing (helps spot "one endpoint / one product").
        "errors_by_operation": f"sum by (span_name) ({_rate(f'{CALLS}{{{s},{ERROR}}}', window)})",
        # From the raw counter: container_cpu_utilization_ratio drifts like a running average.
        "cpu_cores": f"sum(rate(container_cpu_usage_nanoseconds_total{{{c}}}[1m])) / 1e9",
        "memory_bytes": f"avg(container_memory_usage_total_bytes{{{c}}})",
        "memory_percent": f"avg(container_memory_percent_ratio{{{c}}})",
    }


TEMPLATES = list(_service_templates("x", "5m"))


# --- Flagging ----------------------------------------------------------------

def _worse(now, normal, floor):
    """Flag when now is >= worse_factor x its own normal and above the floor."""
    if now is None or now < floor:
        return False
    if not normal:  # nothing normally happens here, so being above the floor is already abnormal
        return True
    return now >= CFG["worse_factor"] * normal


def _steady_rise(values):
    """True if a series rose by trend_min_rise overall, going up on most steps (slow leaks)."""
    if len(values) < 4 or values[0] <= 0:
        return False
    rising = sum(1 for a, b in zip(values, values[1:]) if b > a) / (len(values) - 1)
    return (values[-1] - values[0]) / values[0] >= CFG["trend_min_rise"] and rising >= CFG["trend_min_rising_share"]


TREND_QUERIES = {
    "memory": ("sum by (container_name) (container_memory_usage_total_bytes)",
               lambda v: v[-1] - v[0] >= CFG["trend_memory_min_rise_bytes"]),
    "CPU": ("sum by (container_name) (rate(container_cpu_usage_nanoseconds_total[1m])) / 1e9",
            lambda v: v[-1] >= CFG["trend_cpu_floor_cores"]),
}


def _rising_containers(kind):
    promql, big_enough = TREND_QUERIES[kind]
    rising = set()
    for r in prom_query_range(promql, CFG["trend_minutes"]):
        name = r["metric"].get("container_name")
        values = [float(v) for _, v in r["values"]]
        if name and name not in EXCLUDED_SERVICES and _steady_rise(values) and big_enough(values):
            rising.add(name)
    return rising


# --- Tools (LLM-facing) ------------------------------------------------------

def get_service_overview(window=None):
    """Every service: traffic, error %, latency, each vs its own normal. Worst first, flagged ones marked."""
    window = check_window(window or CFG["default_window"], CFG["max_window_minutes"])
    baseline_window = f"{CFG['baseline_minutes']}m"
    offset = f" offset {CFG['baseline_skip_minutes']}m"
    now_q, base_q = _queries(window), _queries(baseline_window, offset)
    query = {"now": now_q, "baseline": base_q,
             "baseline_window": f"{baseline_window} ending {CFG['baseline_skip_minutes']}m ago"}
    try:
        now = {k: _by(prom_query(q), "service_name") for k, q in now_q.items()}
        base = {k: _by(prom_query(base_q[k]), "service_name") for k in ("request_rate", "error_ratio", "p95_ms")}
        memory_rising = _rising_containers("memory")
        cpu_rising = _rising_containers("CPU")
    except (httpx.HTTPError, RuntimeError) as exc:
        return tool_result("get_service_overview", query, "Prometheus unavailable", error=str(exc))

    rows = []
    for svc in sorted(now["request_rate"]):
        row = {"service": svc, "window": window}
        for k in now:
            row[k] = round(now[k].get(svc, 0.0), 4)
        row["baseline_request_rate"] = round(base["request_rate"].get(svc, 0.0), 4)
        row["baseline_error_ratio"] = round(base["error_ratio"].get(svc, 0.0), 4)
        row["baseline_p95_ms"] = round(base["p95_ms"].get(svc, 0.0), 1)
        reasons = []
        if _worse(row["error_ratio"], row["baseline_error_ratio"], CFG["error_ratio_floor"]):
            reasons.append(f"errors {row['error_ratio']:.1%} vs normal {row['baseline_error_ratio']:.1%}")
        if _worse(row["p95_ms"], row["baseline_p95_ms"], CFG["p95_floor_ms"]):
            reasons.append(f"p95 {row['p95_ms']:.0f}ms vs normal {row['baseline_p95_ms']:.0f}ms")
        if _worse(row["request_rate"], row["baseline_request_rate"], CFG["request_rate_floor"]):
            reasons.append(f"traffic {row['request_rate']:.1f}/s vs normal {row['baseline_request_rate']:.1f}/s")
        if svc in memory_rising:
            reasons.append(f"memory rising steadily over {CFG['trend_minutes']}m")
        if svc in cpu_rising:
            reasons.append(f"CPU rising steadily over {CFG['trend_minutes']}m")
        row["flagged"] = bool(reasons)
        row["reasons"] = reasons
        rows.append(row)

    rows.sort(key=lambda r: (not r["flagged"], -r["error_ratio"], -r["p95_ms"]))
    flagged = [r for r in rows if r["flagged"]]
    lines = [f"{len(rows)} services, {len(flagged)} flagged (window {window}, normal = {query['baseline_window']})."]
    for r in rows:
        mark = "FLAG " if r["flagged"] else "     "
        lines.append(f"{mark}{r['service']:16} {r['request_rate']:7.2f} req/s  err {r['error_ratio']:6.1%}  "
                     f"p95 {r['p95_ms']:7.0f}ms" + (f"  <- {'; '.join(r['reasons'])}" if r["reasons"] else ""))
    facts = [key_fact("metric", r["service"], **{k: v for k, v in r.items() if k != "service"}) for r in rows]
    return tool_result("get_service_overview", query, "\n".join(lines), facts, raw=rows)


def query_metric(template, service, window=None, range_minutes=None):
    """One signal for one service from a fixed template list. With range_minutes, returns the trend."""
    try:
        service = check_service(service)
        window = check_window(window or CFG["default_window"], CFG["max_window_minutes"])
        templates = _service_templates(service, window)
        if template not in templates:
            raise ToolInputError(f"unknown template {template!r}; choose from {TEMPLATES}")
        if range_minutes is not None and not 1 <= int(range_minutes) <= CFG["max_window_minutes"]:
            raise ToolInputError(f"range_minutes must be 1..{CFG['max_window_minutes']}")
    except ToolInputError as exc:
        return tool_result("query_metric", {"template": template, "service": service}, "invalid input", error=str(exc))

    promql = templates[template]
    query = {"template": template, "service": service, "window": window,
             "range_minutes": range_minutes, "promql": promql}
    try:
        if range_minutes:
            results = prom_query_range(promql, int(range_minutes))
        else:
            results = prom_query(promql)
    except (httpx.HTTPError, RuntimeError) as exc:
        return tool_result("query_metric", query, "Prometheus unavailable", error=str(exc))

    if not results:
        return tool_result("query_metric", query, f"{template} for {service}: no data", raw=[])

    facts, lines = [], []
    for r in results:
        label = r["metric"].get("span_name", service)
        if range_minutes:
            values = [float(v) for _, v in r["values"] if v not in ("NaN", "+Inf")]
            if not values:
                continue
            trend = {"first": round(values[0], 4), "last": round(values[-1], 4),
                     "min": round(min(values), 4), "max": round(max(values), 4),
                     "steady_rise": _steady_rise(values), "points": len(values)}
            facts.append(key_fact("metric", service, name=template, label=label, range_minutes=range_minutes, **trend))
            lines.append(f"{label}: {trend['first']} -> {trend['last']} (min {trend['min']}, max {trend['max']})"
                         + (" steadily rising" if trend["steady_rise"] else ""))
        else:
            value = float(r["value"][1])
            if value != value:  # NaN: no traffic in the window
                continue
            facts.append(key_fact("metric", service, name=template, label=label, value=round(value, 4), window=window))
            lines.append(f"{label}: {value:.4g}")
    summary = f"{template} for {service} ({f'last {range_minutes}m' if range_minutes else window}):\n" + \
              ("\n".join(lines) or "no data")
    return tool_result("query_metric", query, summary, facts, raw=results)


def list_metric_names(contains=None):
    """Metric names Prometheus knows about, optionally filtered by a substring."""
    try:
        names = _get("/api/v1/label/__name__/values", {})
    except (httpx.HTTPError, RuntimeError) as exc:
        return tool_result("list_metric_names", {"contains": contains}, "Prometheus unavailable", error=str(exc))
    if contains:
        names = [n for n in names if contains.lower() in n.lower()]
    return tool_result("list_metric_names", {"contains": contains},
                       f"{len(names)} metrics" + (f" matching '{contains}'" if contains else "") + ": " + ", ".join(names[:80]),
                       [key_fact("metric", None, names=names)], raw=names)
