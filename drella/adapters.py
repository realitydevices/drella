"""Model adapters, one per interface [27] (docs/v0.2.md#adapters).

An adapter turns a model (interface, base URL, model ID, ...) into what the job needs
to use it: OpenCode's provider config, and a preflight check the wrapper runs before
starting the agent. Adding a provider type means adding an adapter here; adding a model
is just a row in the models table.

The API key never appears in what's rendered here. OpenCode's config refers to it as
{env:DRELLA_LLM_KEY}, and preflight headers as {key}; the wrapper fills both in from the
environment.

OpenCode substitutes {env:...} and {file:...} anywhere in its config's raw text, so
nothing from a model row may contain "{" or "}" (the API rejects them), or a model row
could read the pod's environment or files.
"""

from dataclasses import dataclass
from enum import StrEnum

# Env var the job's model key is in, and the Secret key it comes from.
LLM_KEY_ENV = "DRELLA_LLM_KEY"
LLM_KEY_SECRET = "_llm-key"
# Placeholder for the key in preflight header values.
KEY_PLACEHOLDER = "{key}"
# OpenCode's provider name for the job's model: the agent runs "drella/<model_id>".
PROVIDER = "drella"


class Interface(StrEnum):
    OPENAI_COMPATIBLE = "openai-compatible"
    ANTHROPIC = "anthropic"


@dataclass(frozen=True)
class ModelSpec:
    """A job's model, as snapshotted when the job was created."""

    alias: str
    interface: Interface
    base_url: str
    model_id: str
    has_key: bool = False
    context_length: int | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class Preflight:
    url: str
    headers: tuple[str, ...] = ()  # "Name: value", with {key} for the API key


# AI SDK package OpenCode loads for each interface.
_NPM = {
    Interface.OPENAI_COMPATIBLE: "@ai-sdk/openai-compatible",
    Interface.ANTHROPIC: "@ai-sdk/anthropic",
}


def opencode_config(m: ModelSpec) -> dict:
    """OpenCode's config for the job: one provider, one model."""
    options: dict = {"baseURL": m.base_url}
    if m.has_key:
        options["apiKey"] = "{env:%s}" % LLM_KEY_ENV
    model: dict = {"name": m.model_id}
    # OpenCode takes a model's limits only as a pair (it rejects a context limit with
    # no output limit), so they're passed only when both are known.
    if m.context_length and m.max_output_tokens:
        model["limit"] = {"context": m.context_length, "output": m.max_output_tokens}
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            PROVIDER: {
                "npm": _NPM[m.interface],
                "name": f"Drella: {m.alias}",
                "options": options,
                "models": {m.model_id: model},
            }
        },
        "model": f"{PROVIDER}/{m.model_id}",
        "autoupdate": False,
        "share": "disabled",
    }


def preflight(m: ModelSpec) -> Preflight:
    """A cheap request that fails fast if the URL is wrong or the key is rejected."""
    url = m.base_url.rstrip("/") + "/models"
    if m.interface == Interface.ANTHROPIC:
        headers = ("anthropic-version: 2023-06-01",)
        if m.has_key:
            headers = (f"x-api-key: {KEY_PLACEHOLDER}", *headers)
        return Preflight(url, headers)
    if m.has_key:
        return Preflight(url, (f"Authorization: Bearer {KEY_PLACEHOLDER}",))
    return Preflight(url)
