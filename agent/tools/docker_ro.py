"""Read-only Docker wrapper (owner: B).

R1: the agent has no write tools. Docker is the one capability that could
change the system by accident, so agent code never touches the SDK directly —
it goes through here, and this module exposes only four verbs:

    list_containers / inspect / stats / logs

There is deliberately no start, stop, restart, exec, run, kill or update. If
the agent needs to change something it goes through the Action API (R2), which
is the only path with an approval gate behind it.

Visibility is an ALLOWLIST, not a denylist: only containers belonging to the
demo's compose project are visible, minus the excluded services from config.
That matters for R13 — the test harness starts a short-lived helper container
for network faults, and it is not part of the compose project, so it is
invisible here without us having to recognise it. A denylist would have to know
what to look for; an allowlist does not.

`inspect` strips the environment and mounts: env vars carry endpoint and
toggle configuration, and mounts reveal file paths the agent must not know
about (R4).
"""
from agent.tools.common import EXCLUDED_SERVICES, now_iso, tool_result
from agent.tools.redaction import COMPOSE_PROJECT, redact_lines, redact_mapping

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"

# Returned by inspect; everything else is dropped. An allowlist again, so a new
# Docker version cannot quietly introduce a field that leaks something.
INSPECT_FIELDS = ("Id", "Name", "Created", "State", "RestartCount", "Image")
CONFIG_FIELDS = ("Image", "Labels", "Hostname")

_client = None


class DockerUnavailable(RuntimeError):
    """The daemon is not reachable. A signal for the loop, not a crash."""


def _docker():
    global _client
    if _client is None:
        try:
            import docker
            _client = docker.from_env()
        except Exception as exc:
            raise DockerUnavailable(f"docker is not reachable: {exc}") from exc
    return _client


def set_client(client):
    """Inject a client. Used by tests so they need no daemon."""
    global _client
    _client = client


def _service_of(container):
    return (container.labels or {}).get(COMPOSE_SERVICE_LABEL) or container.name


def _is_visible(container):
    """True only for demo-project containers that are not excluded."""
    labels = container.labels or {}
    if COMPOSE_PROJECT and labels.get(COMPOSE_PROJECT_LABEL) != COMPOSE_PROJECT:
        return False
    return _service_of(container) not in EXCLUDED_SERVICES


def _find(service):
    for c in _docker().containers.list(all=True):
        if _is_visible(c) and _service_of(c) == service:
            return c
    return None


# --- the four read-only verbs ------------------------------------------------

def list_containers():
    """Visible containers with their status. No arguments: nothing to inject."""
    try:
        containers = _docker().containers.list(all=True)
    except DockerUnavailable as exc:
        return tool_result("list_containers", {}, "", error=str(exc))

    rows = [
        {"service": _service_of(c), "status": c.status, "name": c.name}
        for c in containers if _is_visible(c)
    ]
    rows.sort(key=lambda r: r["service"])
    running = sum(1 for r in rows if r["status"] == "running")
    return tool_result(
        "list_containers",
        {},
        f"{len(rows)} containers visible, {running} running, "
        f"{len(rows) - running} not running.",
        key_facts=[],
        raw=rows,
    )


def inspect(service):
    """Container state with environment and mounts removed."""
    try:
        container = _find(service)
    except DockerUnavailable as exc:
        return tool_result("inspect", {"service": service}, "", error=str(exc))
    if container is None:
        return tool_result("inspect", {"service": service}, "",
                           error=f"no visible container for service {service!r}")

    attrs = container.attrs or {}
    out = {k: attrs.get(k) for k in INSPECT_FIELDS if k in attrs}
    config = attrs.get("Config") or {}
    # Env and Mounts are never copied across.
    out["Config"] = {k: config.get(k) for k in CONFIG_FIELDS if k in config}
    out = redact_mapping(out)

    state = out.get("State") or {}
    return tool_result(
        "inspect", {"service": service},
        f"{service}: status={state.get('Status')} exit_code={state.get('ExitCode')} "
        f"oom_killed={state.get('OOMKilled')} restarts={out.get('RestartCount')}",
        raw=out,
    )


def stats(service):
    """One non-streaming resource snapshot."""
    try:
        container = _find(service)
    except DockerUnavailable as exc:
        return tool_result("stats", {"service": service}, "", error=str(exc))
    if container is None:
        return tool_result("stats", {"service": service}, "",
                           error=f"no visible container for service {service!r}")

    snapshot = container.stats(stream=False) or {}
    memory = snapshot.get("memory_stats") or {}
    used, limit = memory.get("usage"), memory.get("limit")
    pct = round(100 * used / limit, 1) if used and limit else None
    return tool_result(
        "stats", {"service": service, "ts": now_iso()},
        f"{service}: memory {used} of {limit} bytes" + (f" ({pct}%)" if pct else ""),
        raw={"memory": memory, "cpu": snapshot.get("cpu_stats")},
    )


def logs(service, tail=200, since_seconds=None):
    """Recent log lines, with evaluation chatter filtered out (R4)."""
    try:
        container = _find(service)
    except DockerUnavailable as exc:
        return tool_result("logs", {"service": service}, "", error=str(exc))
    if container is None:
        return tool_result("logs", {"service": service}, "",
                           error=f"no visible container for service {service!r}")

    kwargs = {"tail": int(tail), "stdout": True, "stderr": True}
    if since_seconds:
        kwargs["since"] = int(since_seconds)
    raw = container.logs(**kwargs)
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)

    lines = redact_lines(text.splitlines())
    return tool_result(
        "logs", {"service": service, "tail": tail},
        f"{service}: {len(lines)} log lines after filtering.",
        raw={"lines": lines},
    )
