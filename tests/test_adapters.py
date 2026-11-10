from drella.adapters import Interface, ModelSpec, opencode_config, preflight

LOCAL = ModelSpec("local", Interface.OPENAI_COMPATIBLE, "http://ollama:11434/v1", "some-model")


def test_openai_compatible_without_a_key():
    cfg = opencode_config(LOCAL)
    provider = cfg["provider"]["drella"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"] == {"baseURL": "http://ollama:11434/v1"}
    assert provider["models"] == {"some-model": {"name": "some-model"}}
    assert cfg["model"] == "drella/some-model"
    p = preflight(LOCAL)
    assert p.url == "http://ollama:11434/v1/models" and p.headers == ()


def test_openai_compatible_with_a_key_and_context_length():
    m = ModelSpec(
        "hosted", Interface.OPENAI_COMPATIBLE, "https://example.com/api/v1/", "org/model",
        has_key=True, context_length=65536,
    )
    provider = opencode_config(m)["provider"]["drella"]
    # The key is referenced, never embedded.
    assert provider["options"]["apiKey"] == "{env:DRELLA_LLM_KEY}"
    assert provider["models"]["org/model"]["limit"] == {"context": 65536, "output": 16384}
    p = preflight(m)
    assert p.url == "https://example.com/api/v1/models"
    assert p.headers == ("Authorization: Bearer {key}",)


def test_anthropic():
    m = ModelSpec(
        "claude", Interface.ANTHROPIC, "https://api.anthropic.com/v1", "claude-x", has_key=True
    )
    cfg = opencode_config(m)
    provider = cfg["provider"]["drella"]
    assert provider["npm"] == "@ai-sdk/anthropic"
    assert provider["options"] == {
        "baseURL": "https://api.anthropic.com/v1",
        "apiKey": "{env:DRELLA_LLM_KEY}",
    }
    assert cfg["model"] == "drella/claude-x"
    p = preflight(m)
    assert p.url == "https://api.anthropic.com/v1/models"
    assert p.headers == ("x-api-key: {key}", "anthropic-version: 2023-06-01")
