"""Typed settings loader for the agent.

Enforces R6: every endpoint the agent talks to must be on this laptop. A config
that points anywhere else is refused at load time rather than at request time,
so there is no code path in which the agent reaches the venue network.

The API key is deliberately NOT part of Settings — Settings gets dumped into
audit logs and reports, and a secret must never ride along. Use get_api_key().
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"
DEFAULT_ENV_PATH = REPO_ROOT / ".env"

# R6. Anything not in here is off-laptop as far as we are concerned.
LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
ALLOWED_SCHEMES = frozenset({"http", "https"})

# The log tool picks a backend by name rather than a URL: OpenSearch runs in the
# demo but its port is not published, so logs are read through the read-only
# Docker wrapper (owner B). These are not endpoints, so R6 does not apply.
LOG_BACKENDS = frozenset({"auto", "docker", "opensearch"})


class NonLocalEndpointError(ValueError):
    """Raised when a configured endpoint is not on localhost (R6)."""


def _require_local(value: str, field: str) -> str:
    """Reject any endpoint that is not a local http(s) URL."""
    parts = urlsplit(value)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise NonLocalEndpointError(
            f"endpoints.{field}: scheme {parts.scheme!r} is not allowed "
            f"(expected one of {sorted(ALLOWED_SCHEMES)}) — got {value!r}"
        )
    host = parts.hostname
    if host not in LOCAL_HOSTNAMES:
        raise NonLocalEndpointError(
            f"endpoints.{field}: host {host!r} is not local (R6: nothing leaves "
            f"this laptop). Allowed hosts: {sorted(LOCAL_HOSTNAMES)} — got {value!r}"
        )
    return value


class Endpoints(BaseModel):
    prometheus: str
    jaeger: str
    action_api: str
    # A backend name from LOG_BACKENDS, or a local URL. Owner B.
    logs: str = "docker"

    @field_validator("prometheus", "jaeger", "action_api")
    @classmethod
    def _local_only(cls, v: str, info: Any) -> str:
        return _require_local(v, info.field_name)

    @field_validator("logs")
    @classmethod
    def _logs_backend_or_local_url(cls, v: str) -> str:
        return v if v in LOG_BACKENDS else _require_local(v, "logs")


class LLMSettings(BaseModel):
    provider: str = "gemini"
    model: str
    fallbacks: list[str] = Field(default_factory=list)
    temperature: float = 0.0
    timeout_s: int = 60
    max_retries: int = 2
    max_cost_usd: float = 5.0

    @property
    def model_chain(self) -> list[str]:
        """Primary model first, then each fallback, de-duplicated in order."""
        seen: dict[str, None] = {}
        for m in [self.model, *self.fallbacks]:
            seen.setdefault(m, None)
        return list(seen)


class InvestigationSettings(BaseModel):
    max_steps: int = 12
    max_tool_output_tokens: int = 1500
    lookback_minutes: int = 15


class PolicySettings(BaseModel):
    min_confidence_to_act: float = 0.7
    high_confidence_requires_types: int = 2


class AlertSettings(BaseModel):
    poll_s: int = 15
    error_rate_threshold: float = 0.05
    p95_latency_ms: int = 1500
    debounce_polls: int = 2


class VerifySettings(BaseModel):
    settle_s: int = 30
    timeout_s: int = 180
    consecutive_ok: int = 3


class ApprovalSettings(BaseModel):
    timeout_s: int = 600


class ModeSettings(BaseModel):
    dry_run_default: bool = True


class Settings(BaseModel):
    endpoints: Endpoints
    excluded_services: list[str] = Field(default_factory=list)
    excluded_labels: list[str] = Field(default_factory=list)
    llm: LLMSettings
    investigation: InvestigationSettings = InvestigationSettings()
    policy: PolicySettings = PolicySettings()
    alert: AlertSettings = AlertSettings()
    verify: VerifySettings = VerifySettings()
    approval: ApprovalSettings = ApprovalSettings()
    mode: ModeSettings = ModeSettings()


def load_env(path: Path | str = DEFAULT_ENV_PATH) -> None:
    """Load KEY=VALUE pairs from .env into os.environ without overriding it.

    Small on purpose: a real dotenv dependency is not worth announcing for this.
    """
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load_settings(path: Path | str = DEFAULT_SETTINGS_PATH) -> Settings:
    """Read settings.yaml and validate it. Raises NonLocalEndpointError on R6."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return settings_from_dict(data)


def settings_from_dict(data: dict[str, Any]) -> Settings:
    """Validate an already-parsed settings mapping.

    Pydantic wraps validator exceptions in a ValidationError, which would bury
    an R6 violation in a wall of generic text. Unwrap it so a misconfigured
    endpoint surfaces as the specific rule it breaks.
    """
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            original = (err.get("ctx") or {}).get("error")
            if isinstance(original, NonLocalEndpointError):
                raise original from exc
            message = str(original) if original is not None else str(err.get("msg", ""))
            if "is not local" in message or "is not allowed" in message:
                raise NonLocalEndpointError(message) from exc
        raise


def get_api_key(provider: str = "gemini") -> str:
    """Fetch the provider key from the environment, loading .env first.

    Kept out of Settings so the key can never be serialised into an audit log
    or an incident report.
    """
    load_env()
    var = {"gemini": "GEMINI_API_KEY"}.get(provider, f"{provider.upper()}_API_KEY")
    key = os.environ.get(var, "")
    if not key:
        raise RuntimeError(f"{var} is not set — copy .env.example to .env and fill it in")
    return key
