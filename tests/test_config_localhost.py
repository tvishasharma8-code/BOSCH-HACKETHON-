"""R6: the agent must never be configurable to talk to anything off this laptop.

Owner C (README section 13). This is the skeleton's smoke test as well as the
rule's enforcement point — if it passes, config loading works end to end.
"""

from __future__ import annotations

import copy

import pytest

from agent.config import (
    DEFAULT_SETTINGS_PATH,
    NonLocalEndpointError,
    load_settings,
    settings_from_dict,
)

VALID = {
    "endpoints": {
        "prometheus": "http://localhost:9090",
        "jaeger": "http://localhost:8080/jaeger",
        "logs": "docker",
        "action_api": "http://127.0.0.1:8700",
    },
    "llm": {"provider": "gemini", "model": "gemini-3.8-flash", "fallbacks": ["gemini-3.5-flash"]},
}


def test_local_endpoints_load():
    s = settings_from_dict(copy.deepcopy(VALID))
    assert s.endpoints.prometheus == "http://localhost:9090"
    assert s.mode.dry_run_default is True


@pytest.mark.parametrize(
    "bad",
    [
        "http://example.com:9090",          # plain remote host
        "http://10.0.0.5:9090",             # private but not this laptop
        "http://127.0.0.1.example.com/",    # localhost-looking, actually remote
        "ftp://localhost:9090",             # local but wrong scheme
    ],
)
def test_non_local_endpoint_is_rejected(bad):
    data = copy.deepcopy(VALID)
    data["endpoints"]["prometheus"] = bad
    with pytest.raises(NonLocalEndpointError):
        settings_from_dict(data)


@pytest.mark.parametrize("backend", ["auto", "docker", "opensearch"])
def test_named_log_backends_are_allowed(backend):
    data = copy.deepcopy(VALID)
    data["endpoints"]["logs"] = backend
    assert settings_from_dict(data).endpoints.logs == backend


def test_a_remote_logs_url_is_still_rejected():
    data = copy.deepcopy(VALID)

    data["endpoints"]["logs"] = "http://example.com:9200"
    with pytest.raises(NonLocalEndpointError):
        settings_from_dict(data)


def test_committed_settings_file_is_valid_and_local():
    """The real config/settings.yaml must itself pass the R6 guard."""
    s = load_settings(DEFAULT_SETTINGS_PATH)
    assert s.llm.model_chain[0] == "gemini-3-flash-preview"
    assert "gemini-2.5-flash" in s.llm.model_chain


def test_model_chain_dedupes_and_keeps_order():
    data = copy.deepcopy(VALID)
    data["llm"]["fallbacks"] = ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-2.5-flash"]
    assert settings_from_dict(data).llm.model_chain == [
        "gemini-3.8-flash",
        "gemini-3.5-flash",
        "gemini-2.5-flash",
    ]
