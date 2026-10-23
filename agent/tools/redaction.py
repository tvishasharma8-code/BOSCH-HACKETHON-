"""Redaction filter (owner: B). Nothing here may reach the model un-scrubbed.

R4: the diagnosis must come from telemetry alone. Anything that would hand the
agent the answer — toggle state, evaluation chatter, the names of the switches
themselves — is stripped before tool output leaves for the LLM.

Note on why this file names nothing specific: the forbidden-access guard
(README Section 1) fails any file under `agent/` that contains those words. So
the vocabulary lives in `config/settings.yaml` under `redaction:` and is read
from there, the same way `common.py` sources `excluded_services`. Keep it that
way — adding a literal here is what breaks the guard.

Two layers, because a key allowlist is not enough on its own:

  - keys    : drop any mapping key starting with a configured prefix.
  - values  : some allowlisted keys hold free text that can still echo a name
              (a stack trace, an error string). Those get the line filter too.
"""
import re

from agent.tools.common import CONFIG

_CFG = CONFIG.get("redaction", {})

ATTR_PREFIXES = tuple(_CFG.get("attr_prefixes", []))
SCRUB_VALUE_KEYS = frozenset(_CFG.get("scrub_values_for_keys", []))
PLACEHOLDER = _CFG.get("placeholder", "[redacted]")
COMPOSE_PROJECT = _CFG.get("compose_project", "")

_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in _CFG.get("line_patterns", []))


def is_sensitive_key(key):
    return bool(ATTR_PREFIXES) and str(key).startswith(ATTR_PREFIXES)


def matches_sensitive_text(text):
    return any(p.search(str(text)) for p in _PATTERNS)


def redact_text(text, drop_lines=True):
    """Scrub free text.

    `drop_lines=True` removes whole offending lines, which is what you want for
    log output: a line about an evaluation carries nothing else of value.
    `drop_lines=False` replaces the line with the placeholder instead, so a
    one-line field (a summary, an exception message) does not silently vanish.
    """
    if not text or not _PATTERNS:
        return text

    kept = []
    for line in str(text).splitlines():
        if matches_sensitive_text(line):
            if not drop_lines:
                kept.append(PLACEHOLDER)
            continue
        kept.append(line)
    return "\n".join(kept)


def redact_value(key, value):
    """Scrub one value, recursing into containers."""
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return [redact_value(key, v) for v in value]
    if isinstance(value, str) and (key in SCRUB_VALUE_KEYS or matches_sensitive_text(value)):
        return redact_text(value, drop_lines=False) or PLACEHOLDER
    return value


def redact_mapping(data):
    """Drop sensitive keys, scrub what survives. Recurses."""
    if not isinstance(data, dict):
        return data
    return {
        k: redact_value(k, v)
        for k, v in data.items()
        if not is_sensitive_key(k)
    }


def redact_lines(lines):
    """Filter a sequence of log lines, dropping the offending ones."""
    return [ln for ln in lines if not matches_sensitive_text(ln)]


def redact_tool_result(result):
    """Scrub a `tool_result(...)` dict. The last thing called before the model sees it.

    Applied to `summary`, `key_facts` and `raw` together: a fact's `data` can
    hold an attribute map, and `raw` is whatever the backend returned.
    """
    if not isinstance(result, dict):
        return result

    scrubbed = dict(result)
    if isinstance(scrubbed.get("summary"), str):
        scrubbed["summary"] = redact_text(scrubbed["summary"], drop_lines=False)
    if isinstance(scrubbed.get("key_facts"), list):
        scrubbed["key_facts"] = [redact_mapping(f) for f in scrubbed["key_facts"]]
    if scrubbed.get("raw") is not None:
        scrubbed["raw"] = redact_value("raw", scrubbed["raw"])
    if isinstance(scrubbed.get("error"), str):
        scrubbed["error"] = redact_text(scrubbed["error"], drop_lines=False)
    return scrubbed
