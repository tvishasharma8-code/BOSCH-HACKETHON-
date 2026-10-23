"""FakeLLM (owner: C) — a scripted LLMProvider for everyone's tests.

Nobody should need a network connection, an API key or a budget to test code
that sits downstream of the model. Script the decisions you want, replay them
in order, then assert on what the loop did with them.

    llm = FakeLLM([
        tool_call("get_service_overview", {"window": "5m"}),
        tool_call("search_traces", {"service": "cart"}),
        finish("cart is failing 75% of GetCart calls [E1, E2]"),
    ])

A `thoughtSignature` is included in every scripted turn so that code which
forgets to round-trip `raw_content` still looks right here and then fails
against the real API. See the trap in provider.py: the fake deliberately does
not hide it.
"""
from agent.llm.provider import LLMTurn, LLMUnavailable, Usage, _parse


def tool_call(name, args=None, call_id=None, signature="fake-signature"):
    """Script one tool-calling turn."""
    part = {"functionCall": {"name": name, "args": args or {}, "id": call_id or f"call_{name}"}}
    if signature:
        part["thoughtSignature"] = signature
    return {"role": "model", "parts": [part]}


def finish(text, signature="fake-signature"):
    """Script one final text turn."""
    part = {"text": text}
    if signature:
        part["thoughtSignature"] = signature
    return {"role": "model", "parts": [part]}


class FakeLLM:
    """Replays scripted turns. Raises if the caller asks for more than scripted."""

    def __init__(self, turns=None, usage_per_call=None, fail_after=None):
        self.scripted = list(turns or [])
        self.usage_per_call = usage_per_call or Usage(100, 20, 40, 160)
        # Set to N to make call N+1 behave like a dead model chain, so callers
        # can prove they escalate instead of crashing.
        self.fail_after = fail_after
        self.calls_made = 0
        self.total_usage = Usage()
        # Everything the loop sent us, for assertions.
        self.received = []

    def call(self, system_instruction, contents, tool_declarations=None):
        self.received.append({
            "system_instruction": system_instruction,
            "contents": list(contents),
            "tool_declarations": list(tool_declarations or []),
        })

        if self.fail_after is not None and self.calls_made >= self.fail_after:
            raise LLMUnavailable("FakeLLM: scripted outage")

        if self.calls_made >= len(self.scripted):
            raise AssertionError(
                f"FakeLLM ran out of script: call {self.calls_made + 1} was not "
                f"scripted ({len(self.scripted)} turns provided). Either the loop "
                f"is taking more steps than expected, or the script is short."
            )

        content = self.scripted[self.calls_made]
        turn = _parse({"candidates": [{"content": content}], "modelVersion": "fake-model"}, "fake-model")
        turn = LLMTurn(
            raw_content=turn.raw_content,
            tool_calls=turn.tool_calls,
            text=turn.text,
            model_version="fake-model",
            model_requested="fake-model",
            usage=self.usage_per_call,
        )
        self.calls_made += 1
        self.total_usage = self.total_usage.plus(self.usage_per_call)
        return turn

    # -- assertion helpers ----------------------------------------------------

    @property
    def last_contents(self):
        return self.received[-1]["contents"] if self.received else []

    def tool_names_called(self):
        """Tool names the script handed out, in order."""
        names = []
        for content in self.scripted[: self.calls_made]:
            for part in content.get("parts", []):
                if "functionCall" in part:
                    names.append(part["functionCall"]["name"])
        return names

    def close(self):
        pass
