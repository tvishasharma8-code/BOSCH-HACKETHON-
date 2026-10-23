"""Tests for the Gemini provider and FakeLLM (owner: C).

No network: an httpx.MockTransport stands in for the API. The response shapes
below are copied from real responses captured by hand during prep, including
the thoughtSignature, so these tests fail if the round-trip rule is broken.
"""
import httpx
import pytest

from agent.llm.fake import FakeLLM, finish, tool_call
from agent.llm.provider import (
    GeminiProvider,
    LLMError,
    LLMUnavailable,
    ToolCall,
    Usage,
    turn_from_tool_result,
    user_turn,
)

SIGNATURE = "CvYBAdHtim9abc"

TOOL_CALL_RESPONSE = {
    "candidates": [{
        "content": {
            "role": "model",
            "parts": [{
                "functionCall": {"name": "search_traces", "args": {"service": "cart"}, "id": "call_1026316"},
                "thoughtSignature": SIGNATURE,
            }],
        }
    }],
    "modelVersion": "gemini-3.6-flash",
    "usageMetadata": {
        "promptTokenCount": 195,
        "candidatesTokenCount": 22,
        "thoughtsTokenCount": 400,
        "totalTokenCount": 617,
    },
}

DECLS = [{"name": "search_traces", "description": "x", "parameters": {"type": "object", "properties": {}}}]


def provider_for(handler, **kw):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    kw.setdefault("model", "gemini-3.8-flash")
    kw.setdefault("fallbacks", ["gemini-3.5-flash"])
    return GeminiProvider(api_key="k", client=client, sleep=lambda _: None, **kw)


# --- parsing -----------------------------------------------------------------

def test_tool_call_is_parsed_and_raw_content_kept_verbatim():
    p = provider_for(lambda r: httpx.Response(200, json=TOOL_CALL_RESPONSE))
    turn = p.call("sys", [user_turn("alert")], DECLS)

    assert turn.wants_tool
    assert turn.tool_calls[0] == ToolCall("search_traces", {"service": "cart"}, "call_1026316")
    # The whole point: the signature must survive so it can be sent back.
    assert turn.raw_content["parts"][0]["thoughtSignature"] == SIGNATURE
    assert turn.raw_content is not None


def test_model_version_reports_what_actually_answered():
    """We ask for 3.8; the API may serve something else. Report the truth."""
    p = provider_for(lambda r: httpx.Response(200, json=TOOL_CALL_RESPONSE))
    turn = p.call("sys", [user_turn("alert")], DECLS)
    assert turn.model_requested == "gemini-3.8-flash"
    assert turn.model_version == "gemini-3.6-flash"


def test_thinking_tokens_are_counted():
    p = provider_for(lambda r: httpx.Response(200, json=TOOL_CALL_RESPONSE))
    turn = p.call("sys", [user_turn("alert")], DECLS)
    assert turn.usage == Usage(195, 22, 400, 617)
    assert p.total_usage.thoughts_tokens == 400
    assert p.calls_made == 1


# --- availability ------------------------------------------------------------

def test_503_falls_through_to_the_next_model():
    seen = []

    def handler(request):
        model = request.url.path.split("/")[-1]
        seen.append(model)
        if "3.8" in model:
            return httpx.Response(503, json={"error": {"message": "high demand"}})
        return httpx.Response(200, json=TOOL_CALL_RESPONSE)

    p = provider_for(handler, max_retries=1)
    turn = p.call("sys", [user_turn("alert")], DECLS)

    assert turn.model_requested == "gemini-3.5-flash"
    # 3.8 tried twice (initial + one retry), then the fallback.
    assert [s for s in seen if "3.8" in s].__len__() == 2


def test_whole_chain_down_raises_unavailable_not_a_crash():
    p = provider_for(lambda r: httpx.Response(503, text="high demand"), max_retries=0)
    with pytest.raises(LLMUnavailable) as exc:
        p.call("sys", [user_turn("alert")], DECLS)
    assert "gemini-3.8-flash" in str(exc.value) and "gemini-3.5-flash" in str(exc.value)


def test_timeout_is_retried_then_falls_through():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json=TOOL_CALL_RESPONSE)

    p = provider_for(handler, max_retries=1)
    assert p.call("sys", [user_turn("a")], DECLS).wants_tool


def test_400_is_not_retried():
    """A 400 is the thought_signature mistake. Retrying only burns budget."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, text="Function call is missing a thought_signature")

    p = provider_for(handler, max_retries=3, fallbacks=[])
    with pytest.raises(LLMError):
        p.call("sys", [user_turn("a")], DECLS)
    assert calls["n"] == 1


def test_missing_api_key_is_refused_up_front():
    with pytest.raises(LLMError):
        GeminiProvider(api_key="", model="gemini-3.8-flash")


# --- turn builders -----------------------------------------------------------

def test_turn_from_tool_result_drops_raw_but_keeps_facts():
    call = ToolCall("search_traces", {"service": "cart"}, "call_1")
    result = {
        "tool": "search_traces",
        "summary": "18 of 20 error traces fail at cart.GetCart",
        "key_facts": [{"type": "trace", "service": "cart", "data": {"ratio": 0.9}}],
        "raw": {"huge": "x" * 10_000},
        "error": None,
    }
    turn = turn_from_tool_result(call, result, evidence_id="E1")
    response = turn["parts"][0]["functionResponse"]["response"]

    assert turn["role"] == "user"
    assert response["evidence_id"] == "E1"
    assert response["key_facts"][0]["service"] == "cart"
    assert "raw" not in response            # would blow the step budget
    assert "error" not in response          # absent, not None, when there is none


def test_tool_error_is_passed_to_the_model_as_a_signal():
    call = ToolCall("search_traces", {}, "call_1")
    result = {"summary": "", "key_facts": [], "error": "jaeger timeout"}
    response = turn_from_tool_result(call, result)["parts"][0]["functionResponse"]["response"]
    assert response["error"] == "jaeger timeout"


# --- FakeLLM -----------------------------------------------------------------

def test_fake_replays_in_order_and_records_what_it_saw():
    llm = FakeLLM([
        tool_call("get_service_overview", {"window": "5m"}),
        finish("cart is failing [E1]"),
    ])

    first = llm.call("sys", [user_turn("alert")], DECLS)
    assert first.tool_calls[0].name == "get_service_overview"

    second = llm.call("sys", [user_turn("alert")], DECLS)
    assert not second.wants_tool and "cart is failing" in second.text

    assert llm.calls_made == 2
    assert llm.received[0]["system_instruction"] == "sys"
    assert llm.total_usage.total_tokens == 320


def test_fake_keeps_a_signature_so_round_trip_bugs_still_surface():
    llm = FakeLLM([tool_call("search_traces", {"service": "cart"})])
    turn = llm.call("sys", [], DECLS)
    assert "thoughtSignature" in turn.raw_content["parts"][0]


def test_fake_can_simulate_an_outage():
    llm = FakeLLM([tool_call("x")], fail_after=0)
    with pytest.raises(LLMUnavailable):
        llm.call("sys", [], DECLS)


def test_fake_complains_loudly_when_the_script_runs_out():
    llm = FakeLLM([finish("done")])
    llm.call("sys", [], DECLS)
    with pytest.raises(AssertionError, match="ran out of script"):
        llm.call("sys", [], DECLS)
