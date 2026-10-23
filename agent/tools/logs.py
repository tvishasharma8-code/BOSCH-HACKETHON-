"""Log tool (owner: B). READ-ONLY. Error and warning lines per service.

Raw logs are the worst thing you can hand an LLM: thousands of near-identical
lines, most of them noise, and the one that matters buried in the middle. So
this tool does the work a human would do first — filter to the levels that
indicate trouble, collapse lines that are the same message with different ids
and timestamps, and report each distinct pattern with a count.

"47x connection refused to valkey-cart:6379" is a diagnosis. Forty-seven
separate lines saying so are a token bill.

Counts also carry real signal the raw lines do not: one error is a blip, a
pattern repeating every second is an outage, and a pattern that starts abruptly
is a change. The model can reason about that; it cannot reason about a wall of
text it had to be truncated to fit.

Lines reach us already filtered for anything the agent must not see — see
`docker_ro.logs`, which applies the redaction pass (R4).
"""
import re

from agent.tools.common import (
    ToolInputError, check_service, key_fact, tool_result,
)
from agent.tools import docker_ro

# The demo runs Go, Java, Python, .NET, Node and Rust services, each with its
# own idea of how to write a log line. Match the common shapes rather than
# assume one format.
_LEVEL_RE = {
    "error": re.compile(
        r'(\berror\b|\berr\b|\bfatal\b|\bpanic\b|\bsevere\b|\bcritical\b'
        r'|"severityText"\s*:\s*"(ERROR|FATAL)"|level[=:"\s]+(error|fatal)'
        # \w* before "exception" so RuntimeException and NullReferenceException
        # match: a camel-cased class name has no word boundary before it.
        r'|\w*exception\b|\btraceback\b)',
        re.IGNORECASE,
    ),
    "warn": re.compile(
        r'(\bwarn(ing)?\b|"severityText"\s*:\s*"WARN"|level[=:"\s]+warn)',
        re.IGNORECASE,
    ),
}

# Collapse the parts that differ between two instances of the same message.
_NORMALISE = (
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<id>"),
    (re.compile(r"\b\d+(\.\d+)?(ms|s|us|ns|kb|mb|gb)?\b", re.I), "<n>"),
    (re.compile(r"0x[0-9a-f]+", re.I), "<addr>"),
)

MAX_PATTERNS = 8          # distinct patterns returned; the rest are summarised
MAX_EXAMPLE_CHARS = 220   # one example line per pattern, truncated
DEFAULT_TAIL = 500        # lines pulled from the container before filtering


# A structured line's own level field wins over keyword guessing: a JSON line
# with "level":"warn" that contains an "err": object is a warning, not an error.
_EXPLICIT_LEVEL_RE = re.compile(
    r'"(?:level|severityText|severity)"\s*:\s*"(\w+)"', re.IGNORECASE)
_EXPLICIT_LEVELS = {
    "error": "error", "err": "error", "fatal": "error", "critical": "error", "panic": "error",
    "warn": "warn", "warning": "warn",
    "info": None, "debug": None, "trace": None,
}


def _level_of(line):
    """Highest severity the line looks like, or None."""
    explicit = _EXPLICIT_LEVEL_RE.search(line)
    if explicit and explicit.group(1).lower() in _EXPLICIT_LEVELS:
        return _EXPLICIT_LEVELS[explicit.group(1).lower()]
    if _LEVEL_RE["error"].search(line):
        return "error"
    if _LEVEL_RE["warn"].search(line):
        return "warn"
    return None


def _signature(line):
    """What two instances of the same message have in common."""
    out = line
    for pattern, placeholder in _NORMALISE:
        out = pattern.sub(placeholder, out)
    return " ".join(out.split())[:300]


def _group(lines, levels):
    """Collapse matching lines into {signature: {count, example, level}}."""
    groups = {}
    for line in lines:
        level = _level_of(line)
        if level is None or level not in levels:
            continue
        sig = _signature(line)
        entry = groups.get(sig)
        if entry is None:
            groups[sig] = {
                "count": 1,
                "level": level,
                "example": line.strip()[:MAX_EXAMPLE_CHARS],
            }
        else:
            entry["count"] += 1
            # An error outranks a warning for the same shape of message.
            if level == "error":
                entry["level"] = "error"
    return groups


def search_logs(service, level="error", tail=DEFAULT_TAIL, max_patterns=MAX_PATTERNS):
    """Error/warning lines for one service, deduplicated with counts.

    level: "error" (default), "warn", or "all".
    """
    query = {"service": service, "level": level, "tail": tail}
    # Bad input comes back as an error result, like every other tool, so the
    # agent loop can read it and try again instead of crashing.
    try:
        check_service(service)
        if level not in ("error", "warn", "all"):
            raise ToolInputError(f"level must be error, warn or all — got {level!r}")
    except ToolInputError as exc:
        return tool_result("search_logs", query, "invalid input", error=str(exc))
    levels = ("error", "warn") if level == "all" else (level,)

    raw = docker_ro.logs(service, tail=tail)
    if raw.get("error"):
        return tool_result("search_logs", query, "", error=raw["error"])

    lines = (raw.get("raw") or {}).get("lines") or []
    groups = _group(lines, levels)

    if not groups:
        # A real and useful answer: this service is not complaining. It helps
        # rule a service out, so say it plainly rather than returning nothing.
        return tool_result(
            "search_logs", query,
            f"{service}: no {level} lines in the last {len(lines)} log lines.",
            key_facts=[key_fact("log", service, matched=0, scanned=len(lines), level=level)],
            raw={"patterns": []},
        )

    ranked = sorted(groups.items(), key=lambda kv: kv[1]["count"], reverse=True)
    shown, hidden = ranked[:max_patterns], ranked[max_patterns:]
    matched = sum(g["count"] for _, g in ranked)

    facts = [
        key_fact("log", service,
                 count=g["count"], level=g["level"], example=g["example"], pattern=sig)
        for sig, g in shown
    ]

    top = "; ".join(f"{g['count']}x {g['example']}" for _, g in shown[:3])
    summary = (
        f"{service}: {matched} {level} lines in the last {len(lines)} scanned, "
        f"{len(ranked)} distinct patterns. Top: {top}"
    )
    if hidden:
        summary += f" (+{len(hidden)} more patterns, {sum(g['count'] for _, g in hidden)} lines)"

    return tool_result("search_logs", query, summary, key_facts=facts,
                       raw={"patterns": [dict(signature=s, **g) for s, g in ranked]})


def list_log_services():
    """Which services have logs available — the usual first call for this tool."""
    listing = docker_ro.list_containers()
    if listing.get("error"):
        return tool_result("list_log_services", {}, "", error=listing["error"])
    services = sorted(row["service"] for row in listing.get("raw") or [])
    return tool_result(
        "list_log_services", {},
        f"{len(services)} services with logs: {', '.join(services)}",
        raw={"services": services},
    )
