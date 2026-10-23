"""LLM provider (owner: C). Gemini over raw REST.

No vendor SDK on purpose: httpx is already how the telemetry tools talk HTTP,
and a small explicit client is easier to explain to a judge than a framework.

Two behaviours here are load-bearing and were established by hand against the
live API before any of this was written:

1. `thought_signature`. Gemini 3.x attaches a `thoughtSignature` to the model
   turn and rejects the NEXT request with HTTP 400 if it is missing:
   "Function call is missing a thought_signature in functionCall parts."
   The caller must therefore append the model turn VERBATIM to `contents`.
   That is why `LLMTurn` keeps `raw_content` next to the parsed view: the loop
   appends the former and reads the latter. Rebuilding the turn from
   `name`/`args` passes the first tool call and fails the second.

2. Availability. 503 "high demand" and silent timeouts are this provider's
   normal failure mode, so retry-then-fall-down-the-model-chain is part of the
   contract rather than a refinement. When the whole chain is exhausted we
   raise LLMUnavailable and the investigator escalates. We never invent an
   answer and never crash the run.
"""
import os
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from dotenv import load_dotenv

from agent.config import load_settings

# The key lives in .env; load it here so the provider works however it's imported.
load_dotenv()

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Worth another attempt: overloaded, rate limited, or a transient upstream fault.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class LLMError(RuntimeError):
    """A failure the provider will not retry — a bad key, a malformed request."""


class LLMUnavailable(LLMError):
    """Every model in the chain failed. The caller escalates; it must not guess."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict
    id: str | None = None


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    candidates_tokens: int = 0
    thoughts_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_metadata(cls, meta):
        meta = meta or {}
        return cls(
            prompt_tokens=int(meta.get("promptTokenCount") or 0),
            candidates_tokens=int(meta.get("candidatesTokenCount") or 0),
            # Thinking is on by default and dominated a measured call
            # (400 of 617 tokens), so it has to be in the budget.
            thoughts_tokens=int(meta.get("thoughtsTokenCount") or 0),
            total_tokens=int(meta.get("totalTokenCount") or 0),
        )

    def plus(self, other):
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.candidates_tokens + other.candidates_tokens,
            self.thoughts_tokens + other.thoughts_tokens,
            self.total_tokens + other.total_tokens,
        )


@dataclass(frozen=True)
class LLMTurn:
    """One model turn: the verbatim content plus a parsed view of it."""

    raw_content: dict
    tool_calls: tuple = ()
    text: str = ""
    # The model that actually answered. A request for gemini-3.5-flash came
    # back served by gemini-3.6-flash, so the report must cite this rather
    # than the model we asked for.
    model_version: str = ""
    model_requested: str = ""
    usage: Usage = field(default_factory=Usage)

    @property
    def wants_tool(self):
        return bool(self.tool_calls)


class LLMProvider(Protocol):
    """Implemented by GeminiProvider and FakeLLM."""

    def call(self, system_instruction, contents, tool_declarations=None) -> LLMTurn:
        ...


# --- turn builders -----------------------------------------------------------

def user_turn(text):
    return {"role": "user", "parts": [{"text": text}]}


def tool_response_turn(name, call_id, response):
    """The turn that hands one tool's output back to the model.

    `response` is free-form, so the evidence envelope goes straight in.
    """
    function_response = {"name": name, "response": response}
    if call_id is not None:
        function_response["id"] = call_id
    return {"role": "user", "parts": [{"functionResponse": function_response}]}


def turn_from_tool_result(call, result, evidence_id=None):
    """Build the reply turn from a tool's `tool_result(...)` dict.

    `raw` is deliberately dropped: it is the full payload, it is already saved
    to disk by the evidence store, and sending it would blow the step budget.
    The model gets the summary and the checkable atoms, which is what it needs
    to choose the next tool and what the validator will hold it to.
    """
    payload = {
        "summary": result.get("summary"),
        "key_facts": result.get("key_facts") or [],
    }
    if evidence_id:
        payload["evidence_id"] = evidence_id
    if result.get("error"):
        # A tool failure is a signal to reason about, not a crash.
        payload["error"] = result["error"]
    return tool_response_turn(call.name, call.id, payload)


# --- parsing -----------------------------------------------------------------

def _parse(payload, requested):
    candidates = payload.get("candidates") or []
    content = candidates[0].get("content", {}) if candidates else {}
    calls, texts = [], []

    for part in content.get("parts") or []:
        if "functionCall" in part:
            fc = part["functionCall"]
            calls.append(ToolCall(fc.get("name", ""), fc.get("args") or {}, fc.get("id")))
        elif part.get("text"):
            texts.append(part["text"])

    return LLMTurn(
        raw_content=content,  # verbatim — thoughtSignature and all
        tool_calls=tuple(calls),
        text="\n".join(texts).strip(),
        model_version=payload.get("modelVersion", ""),
        model_requested=requested,
        usage=Usage.from_metadata(payload.get("usageMetadata")),
    )


# --- provider ----------------------------------------------------------------

class GeminiProvider:
    def __init__(self, api_key, model, fallbacks=(), temperature=0.0,
                 timeout_s=60, max_retries=2, base_delay_s=1.0,
                 client=None, sleep=time.sleep):
        if not api_key:
            raise LLMError("no API key: set GEMINI_API_KEY in .env")
        self.api_key = api_key
        # Primary first, then fallbacks, de-duplicated, order preserved.
        self.model_chain = list(dict.fromkeys([model, *fallbacks]))
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.base_delay_s = base_delay_s
        self._client = client or httpx.Client(timeout=timeout_s)
        self._sleep = sleep
        # Running totals, so the loop can enforce a budget and the report can
        # state what the run actually cost.
        self.calls_made = 0
        self.total_usage = Usage()

    @classmethod
    def from_settings(cls, settings=None, **overrides):
        settings = settings or load_settings()
        llm = settings.llm
        # Either name works: the team's .env.example shipped LLM_API_KEY before
        # we settled on Gemini.
        key = os.getenv("GEMINI_API_KEY") or os.getenv("LLM_API_KEY") or ""
        return cls(
            api_key=key,
            model=llm.model,
            fallbacks=llm.fallbacks,
            temperature=llm.temperature,
            timeout_s=llm.timeout_s,
            max_retries=llm.max_retries,
            **overrides,
        )

    def call(self, system_instruction, contents, tool_declarations=None):
        body = self._body(system_instruction, contents, tool_declarations)
        failures = []

        for model in self.model_chain:
            try:
                payload = self._post_with_retry(model, body)
            except LLMError as exc:
                failures.append(f"{model}: {exc}")
                continue
            turn = _parse(payload, requested=model)
            self.calls_made += 1
            self.total_usage = self.total_usage.plus(turn.usage)
            return turn

        raise LLMUnavailable("every model in the chain failed — " + " | ".join(failures))

    def close(self):
        self._client.close()

    # -- internals ------------------------------------------------------------

    def _body(self, system_instruction, contents, tool_declarations):
        body = {
            "contents": list(contents),
            "generationConfig": {"temperature": self.temperature},
        }
        if system_instruction:
            body["system_instruction"] = {"parts": [{"text": system_instruction}]}
        if tool_declarations:
            # Declaration order must stay stable across steps.
            body["tools"] = [{"function_declarations": list(tool_declarations)}]
        return body

    def _post_with_retry(self, model, body):
        url = f"{BASE_URL}/{model}:generateContent"
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}
        last = "no attempt made"

        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.post(url, headers=headers, json=body, timeout=self.timeout_s)
            except httpx.HTTPError as exc:  # timeout, connect error, read error
                last = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    return resp.json()
                last = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code not in RETRYABLE_STATUS:
                    # A 400 here is usually the thought_signature mistake.
                    # Retrying it would only burn the budget.
                    raise LLMError(last)

            if attempt < self.max_retries:
                self._sleep(self.base_delay_s * (2 ** attempt))

        raise LLMError(last)
