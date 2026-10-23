"""Log tool (owner: B).

The behaviour that earns this tool its place is deduplication with counts, so
that is what most of these assert. Docker is faked; no daemon needed.
"""
import pytest

from agent.tools import docker_ro, logs
from agent.tools.redaction import COMPOSE_PROJECT
from tests.test_redaction import FakeClient, FakeContainer


def with_logs(text, service="cart"):
    docker_ro.set_client(FakeClient([FakeContainer(service, service, COMPOSE_PROJECT, log_text=text)]))


@pytest.fixture(autouse=True)
def _reset():
    yield
    docker_ro.set_client(None)


# --- deduplication, the whole point ------------------------------------------

def test_repeated_errors_collapse_into_one_pattern_with_a_count():
    with_logs("\n".join(
        f"2026-01-01T10:00:{i:02d}Z ERROR cart: connection refused to valkey-cart:6379 (attempt {i})"
        for i in range(30)
    ))
    result = logs.search_logs("cart")
    patterns = result["raw"]["patterns"]

    assert len(patterns) == 1, "30 instances of one message should be one pattern"
    assert patterns[0]["count"] == 30
    assert "connection refused" in patterns[0]["example"]
    assert "30x" in result["summary"]


def test_distinct_messages_stay_distinct_and_rank_by_count():
    lines = ["ERROR cart: connection refused"] * 10 + ["ERROR cart: deadline exceeded"] * 3
    with_logs("\n".join(lines))
    patterns = logs.search_logs("cart")["raw"]["patterns"]

    assert [p["count"] for p in patterns] == [10, 3]
    assert "connection refused" in patterns[0]["example"]


def test_timestamps_ids_and_numbers_do_not_split_a_pattern():
    with_logs("\n".join([
        "2026-01-01T10:00:01Z ERROR trace 9f2b1c4d5e6f7a8b order 41 failed after 120ms",
        "2026-01-01T10:00:02Z ERROR trace 1a2b3c4d5e6f7a8b order 42 failed after 95ms",
        "2026-01-01T10:00:03Z ERROR trace 7c8d9e0f1a2b3c4d order 43 failed after 310ms",
    ]))
    patterns = logs.search_logs("cart")["raw"]["patterns"]
    assert len(patterns) == 1 and patterns[0]["count"] == 3


# --- level handling -----------------------------------------------------------

def test_info_lines_are_ignored_and_warnings_excluded_by_default():
    with_logs("\n".join([
        "INFO cart: order placed",
        "WARN cart: retrying",
        "ERROR cart: failed",
    ]))
    assert logs.search_logs("cart", level="error")["raw"]["patterns"][0]["example"].endswith("failed")
    assert len(logs.search_logs("cart", level="error")["raw"]["patterns"]) == 1
    assert len(logs.search_logs("cart", level="all")["raw"]["patterns"]) == 2


@pytest.mark.parametrize("line", [
    'level=error msg="boom"',
    '{"severityText":"ERROR","body":"boom"}',
    "PANIC: nil pointer",
    "java.lang.RuntimeException: boom",
    "Traceback (most recent call last):",
])
def test_error_shapes_across_runtimes_are_recognised(line):
    """The demo runs Go, Java, Python, .NET and Node; each logs differently."""
    with_logs(line)
    assert logs.search_logs("cart")["raw"]["patterns"], f"not matched: {line}"


# --- quiet service, which is a real answer -------------------------------------

def test_a_quiet_service_says_so_rather_than_returning_nothing():
    with_logs("INFO cart: all good\nINFO cart: still good")
    result = logs.search_logs("cart")
    assert "no error lines" in result["summary"]
    assert result["key_facts"][0]["data"]["matched"] == 0
    assert result["key_facts"][0]["data"]["scanned"] == 2


# --- budget -------------------------------------------------------------------

def test_pattern_count_is_capped_and_the_remainder_is_reported():
    lines = [f"ERROR cart: distinct failure mode {chr(97 + i)}" for i in range(12)]
    with_logs("\n".join(lines))
    result = logs.search_logs("cart", max_patterns=3)

    assert len(result["key_facts"]) == 3
    assert "+9 more patterns" in result["summary"]
    # raw keeps everything for the evidence store; only the model's view is capped.
    assert len(result["raw"]["patterns"]) == 12


# --- contract -----------------------------------------------------------------

def test_key_facts_are_citable_atoms():
    with_logs("ERROR cart: connection refused\nERROR cart: connection refused")
    fact = logs.search_logs("cart")["key_facts"][0]
    assert fact["type"] == "log" and fact["service"] == "cart"
    assert fact["data"]["count"] == 2 and fact["data"]["level"] == "error"


def test_bad_level_is_rejected():
    with_logs("ERROR x")
    result = logs.search_logs("cart", level="verbose")
    assert result["error"] and "level" in result["error"] and not result["key_facts"]


def test_excluded_service_is_rejected_before_any_docker_call():
    # No fake client is installed, so reaching Docker would surface a Docker error
    # instead of the exclusion message.
    excluded = sorted(docker_ro.EXCLUDED_SERVICES)[0]
    result = logs.search_logs(excluded)
    assert result["error"] and "excluded" in result["error"]


def test_explicit_warn_level_wins_over_err_keyword():
    # Node's pino logger writes warnings as JSON with an "err" object inside.
    with_logs('{"level":"warn","msg":"retrying","err":{"type":"Error"}}\n'
              '{"level":"error","msg":"charge failed"}')
    errors = logs.search_logs("cart", level="error")
    warns = logs.search_logs("cart", level="warn")
    assert [f["data"]["count"] for f in errors["key_facts"]] == [1]
    assert [f["data"]["count"] for f in warns["key_facts"]] == [1]


def test_docker_failure_becomes_an_error_field_not_an_exception():
    docker_ro.set_client(FakeClient([]))
    result = logs.search_logs("cart")
    assert result["error"] and not result["key_facts"]


def test_redaction_still_applies_through_this_path():
    with_logs("ERROR cart: GetCart failed\nDEBUG feature flag cartFailure -> on")
    assert "cartFailure" not in str(logs.search_logs("cart", level="all"))
