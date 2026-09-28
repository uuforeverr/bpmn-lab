from app.config import LlmConfig
from app.llm import LlmClient


def test_thinking_mode_can_be_overridden_per_agent():
    config = LlmConfig(
        thinking="disabled",
        thinking_by_agent={"semantic_resolver": "enabled"},
    )

    assert config.thinking_for("semantic_resolver") == "enabled"
    assert config.thinking_for("planner") == "disabled"


def test_thinking_parameter_and_values_are_provider_configurable():
    config = LlmConfig(
        thinking="enabled",
        thinking_parameter="enable_thinking",
        thinking_enabled_value=True,
        thinking_disabled_value=False,
    )

    assert config.thinking_parameter == "enable_thinking"
    assert config.thinking_value("enabled") is True
    assert config.thinking_value("disabled") is False


def test_openai_compatible_request_features_can_be_omitted_or_renamed():
    config = LlmConfig(
        model="compatible-model",
        temperature=None,
        max_tokens_parameter="max_completion_tokens",
        json_response_format=False,
        parallel_tool_calls=None,
        tool_choice=None,
        thinking="enabled",
        thinking_parameter="enable_thinking",
        thinking_enabled_value=True,
        extra_body={"provider_option": "value"},
        extra_body_by_agent={"planner": {"agent_option": 1}},
    )
    client = object.__new__(LlmClient)
    client.config = config

    kwargs = client._request_kwargs([{"role": "user", "content": "test"}], "planner", "enabled")

    assert "temperature" not in kwargs
    assert "max_tokens" not in kwargs
    assert kwargs["max_completion_tokens"] == 8192
    assert kwargs["extra_body"] == {
        "provider_option": "value",
        "agent_option": 1,
        "enable_thinking": True,
    }
