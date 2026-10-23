"""Redaction and read-only Docker wrapper (owner: B).

R4: toggle state must never reach the model. R13: the harness's helper
containers must be invisible to agent tools. R1: the wrapper cannot mutate.

No Docker daemon required — the client is faked, since the demo runs on a
different machine from most of these test runs.
"""
import pathlib

import pytest

from agent.tools import docker_ro, redaction

AGENT_DIR = pathlib.Path(__file__).resolve().parents[1] / "agent"

# These must not appear in agent/ source. Spelled in pieces so this test file
# does not itself trip the guard it is enforcing.
FORBIDDEN = [
    "flag" + "d",
    "open" + "feature",
    "/" + "feature",
    "/" + "flagservice",
    "demo." + "flagd.json",
    "." + "faultctl",
    "answer." + "json",
    "ground_" + "truth",
]


# --- the guard this task exists to keep green --------------------------------

@pytest.mark.parametrize("word", FORBIDDEN)
def test_no_agent_source_names_a_forbidden_string(word):
    """Narrow stand-in for A's full forbidden-access test (README Section 1).

    The vocabulary belongs in config/settings.yaml, never in agent code.
    """
    offenders = [
        f"{p.relative_to(AGENT_DIR)}"
        for p in AGENT_DIR.rglob("*.py")
        if word.lower() in p.read_text(encoding="utf-8").lower()
    ]
    assert not offenders, f"{word!r} found in agent/: {offenders}"


# --- text ---------------------------------------------------------------------

def test_evaluation_chatter_does_not_survive():
    log = "\n".join([
        "2026-01-01 INFO  checkout: order 123 placed",
        "2026-01-01 DEBUG feature_flag evaluated: cartFailure -> on",
        "2026-01-01 WARN  cart: GetCart returned UNAVAILABLE",
    ])
    out = redaction.redact_text(log)
    assert "order 123 placed" in out
    assert "UNAVAILABLE" in out
    assert "cartFailure" not in out


def test_single_line_field_is_replaced_not_silently_emptied():
    out = redaction.redact_text("flag evaluation says on", drop_lines=False)
    assert out == redaction.PLACEHOLDER


def test_redact_lines_filters_a_sequence():
    kept = redaction.redact_lines(["normal line", "feature flag: on", "another"])
    assert kept == ["normal line", "another"]


# --- mappings -----------------------------------------------------------------

def test_sensitive_keys_are_dropped_including_nested():
    data = {
        "http.status_code": 500,
        "feature_flag.key": "cartFailure",
        "nested": {"feature_flag.variant": "on", "rpc.method": "GetCart"},
    }
    out = redaction.redact_mapping(data)
    assert out["http.status_code"] == 500
    assert "feature_flag.key" not in out
    assert "feature_flag.variant" not in out["nested"]
    assert out["nested"]["rpc.method"] == "GetCart"


def test_allowlisted_free_text_value_is_still_scrubbed():
    """exception.message is allowlisted by key but can echo a name in its text."""
    out = redaction.redact_mapping(
        {"exception.message": "failed because feature flag cartFailure was on"}
    )
    assert "cartFailure" not in out["exception.message"]


# --- tool results -------------------------------------------------------------

def test_redact_tool_result_covers_summary_facts_and_raw():
    result = {
        "tool": "search_traces",
        "summary": "cart failing; feature flag cartFailure enabled",
        "key_facts": [{"type": "trace", "service": "cart",
                       "data": {"feature_flag.key": "cartFailure", "ratio": 0.9}}],
        "raw": {"spans": [{"feature_flag.variant": "on", "name": "GetCart"}]},
        "error": None,
    }
    out = redaction.redact_tool_result(result)

    assert "cartFailure" not in out["summary"]
    assert "feature_flag.key" not in out["key_facts"][0]["data"]
    assert out["key_facts"][0]["data"]["ratio"] == 0.9
    assert "feature_flag.variant" not in out["raw"]["spans"][0]
    assert out["raw"]["spans"][0]["name"] == "GetCart"


# --- docker wrapper -----------------------------------------------------------

class FakeContainer:
    def __init__(self, name, service, project, status="running", log_text=""):
        self.name = name
        self.status = status
        self.labels = {}
        if project:
            self.labels["com.docker.compose.project"] = project
        if service:
            self.labels["com.docker.compose.service"] = service
        self.attrs = {
            "Id": "abc", "Name": "/" + name, "RestartCount": 2,
            "State": {"Status": status, "ExitCode": 0, "OOMKilled": False},
            "Config": {
                "Image": "demo:latest",
                "Hostname": name,
                "Env": ["SECRET=hunter2", "FLAG_HOST=somewhere"],
            },
            "Mounts": [{"Source": "/host/src", "Destination": "/etc/cfg"}],
        }
        self._log_text = log_text

    def logs(self, **kw):
        return self._log_text.encode()

    def stats(self, stream=False):
        return {"memory_stats": {"usage": 500, "limit": 1000}, "cpu_stats": {}}


class FakeClient:
    def __init__(self, containers):
        self.containers = self
        self._containers = containers

    def list(self, all=False):
        return self._containers


PROJECT = redaction.COMPOSE_PROJECT


@pytest.fixture(autouse=True)
def _reset_client():
    yield
    docker_ro.set_client(None)


def test_wrapper_exposes_no_mutating_verb():
    """R1: there must be no way to change anything through this module."""
    for verb in ("start", "stop", "restart", "kill", "exec_run", "run", "update", "remove"):
        assert not hasattr(docker_ro, verb), f"docker_ro exposes {verb}"


def test_helper_container_outside_the_project_is_invisible():
    """R13: the harness's netem helper runs unlabelled, so it must not appear."""
    docker_ro.set_client(FakeClient([
        FakeContainer("cart", "cart", PROJECT),
        FakeContainer("eager_netshoot", None, None),   # the helper
    ]))
    services = [r["service"] for r in docker_ro.list_containers()["raw"]]
    assert services == ["cart"]


def test_excluded_services_are_invisible():
    docker_ro.set_client(FakeClient([
        FakeContainer("cart", "cart", PROJECT),
        FakeContainer("x1", sorted(docker_ro.EXCLUDED_SERVICES)[0], PROJECT),
    ]))
    services = [r["service"] for r in docker_ro.list_containers()["raw"]]
    assert services == ["cart"]


def test_inspect_drops_env_and_mounts():
    docker_ro.set_client(FakeClient([FakeContainer("cart", "cart", PROJECT)]))
    raw = docker_ro.inspect("cart")["raw"]

    assert "Env" not in raw["Config"]
    assert "Mounts" not in raw
    assert "hunter2" not in str(raw)
    assert raw["State"]["Status"] == "running"
    assert raw["RestartCount"] == 2


def test_logs_are_filtered():
    text = "cart: GetCart failed\nDEBUG feature flag cartFailure -> on\ncart: retrying"
    docker_ro.set_client(FakeClient([FakeContainer("cart", "cart", PROJECT, log_text=text)]))
    lines = docker_ro.logs("cart")["raw"]["lines"]
    assert lines == ["cart: GetCart failed", "cart: retrying"]


def test_unknown_service_returns_an_error_not_an_exception():
    docker_ro.set_client(FakeClient([]))
    result = docker_ro.inspect("nope")
    assert result["error"] and "nope" in result["error"]
