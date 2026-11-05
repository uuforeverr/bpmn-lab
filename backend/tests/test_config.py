import pytest
from pydantic import ValidationError

from app.config import LlmConfig, PipelineConfig, PromptConfig, Settings, load_settings
from app.llm import LlmClient, prompt
from app.pipeline import Pipeline


def test_thinking_mode_can_be_overridden_per_agent():
    config = LlmConfig(
        thinking="disabled",
        thinking_by_agent={"planner": "enabled"},
    )

    assert config.thinking_for("planner") == "enabled"
    assert config.thinking_for("generator") == "disabled"


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
        max_output_tokens_by_agent={"planner": 16384},
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
    assert kwargs["max_completion_tokens"] == 16384
    assert kwargs["extra_body"] == {
        "provider_option": "value",
        "agent_option": 1,
        "enable_thinking": True,
    }


def test_reviewer_can_be_disabled_per_pipeline_configuration():
    assert PipelineConfig().reviewer_enabled is False
    assert PipelineConfig(reviewer_enabled=False).reviewer_enabled is False
    assert PipelineConfig(reviewer_enabled=True).reviewer_enabled is True
    assert PipelineConfig().max_review_discussion_rounds == 3
    assert PipelineConfig().max_soundness_repairs == 5


def test_soundness_repair_limit_must_be_non_negative():
    assert PipelineConfig(max_soundness_repairs=0).max_soundness_repairs == 0
    with pytest.raises(ValidationError):
        PipelineConfig(max_soundness_repairs=-1)


def test_arbitrator_can_use_an_independent_stronger_model_configuration():
    settings = Settings(arbitrator_llm=LlmConfig(
        base_url="https://strong.example/v1",
        api_key="secret",
        model="strong-model",
    ))

    assert settings.arbitrator_llm is not None
    assert settings.arbitrator_llm.model == "strong-model"
    assert settings.arbitrator_llm.base_url == "https://strong.example/v1"


def test_prompt_language_is_globally_configurable():
    zh_pipeline = Pipeline(Settings(prompts=PromptConfig(language="zh")))
    en_pipeline = Pipeline(Settings(prompts=PromptConfig(language="en")))

    assert zh_pipeline._prompt("generator") == prompt("generator", "zh")
    assert en_pipeline._prompt("generator") == prompt("generator", "en")
    assert zh_pipeline._prompt("generator") != en_pipeline._prompt("generator")


def test_prompt_language_rejects_unsupported_values():
    with pytest.raises(ValidationError):
        PromptConfig(language="mixed")


def test_every_agent_prompt_has_an_english_counterpart():
    names = {
        "planner", "generator", "reviewer", "arbitrator",
        "generator_revision", "generator_oneshot", "repair_plan", "repair",
    }

    for name in names:
        assert prompt(name, "zh").strip()
        assert prompt(name, "en").strip()


def test_split_join_pairing_rule_is_synchronized_across_generator_prompts():
    for name in {"generator", "generator_oneshot", "generator_revision"}:
        zh = prompt(name, "zh")
        en = prompt(name, "en")
        assert "Parallel Split" in zh and "Parallel Join" in zh, name
        assert "Exclusive Split" in zh and "Exclusive Join" in zh, name
        assert "Inclusive Split" in zh and "Inclusive Join" in zh, name
        assert "Parallel Split" in en and "Parallel Join" in en, name
        assert "Exclusive Split" in en and "Exclusive Join" in en, name
        assert "Inclusive Split" in en and "Inclusive Join" in en, name

def test_loop_return_and_single_entry_rules_are_synchronized_across_prompts():
    for name in {"generator", "repair_plan", "repair"}:
        assert "循环回边" in prompt(name, "zh"), name
        assert "loop-return edge" in prompt(name, "en"), name
        assert "只有" in prompt(name, "zh") and "endEvent" in prompt(name, "zh"), name
        assert "Only Join Gateways and End Events" in prompt(name, "en"), name


def test_semantic_review_prompts_follow_the_standard_section_order():
    expected = [
        "# Role", "# Task Objective", "# Input", "# Domain Knowledge",
        "# Execution Rules", "# Output Format", "# Examples",
    ]
    for language in {"zh", "en"}:
        for name in {"reviewer", "generator_revision"}:
            headings = [
                line.strip() for line in prompt(name, language).splitlines()
                if line.startswith("# ")
            ]
            assert headings == expected, (language, name)


def test_few_shots_are_limited_to_incremental_generation_prompts():
    prompts_with_examples = {"generator": 2, "generator_revision": 1}
    prompts_without_examples = {
        "planner", "reviewer", "arbitrator", "generator_oneshot",
        "repair_plan", "repair",
    }
    for language in {"zh", "en"}:
        for name, expected_count in prompts_with_examples.items():
            assert prompt(name, language).count("## Example") == expected_count
            assert expected_count <= 3
        for name in prompts_without_examples:
            assert "## Example" not in prompt(name, language), (language, name)


def test_agent_output_budget_falls_back_to_global_value():
    config = LlmConfig(max_output_tokens=8192, max_output_tokens_by_agent={"repair_plan": 16384})

    assert config.max_output_tokens_for("repair_plan") == 16384
    assert config.max_output_tokens_for("generator") == 8192

def test_english_json_agents_explicitly_request_json():
    for name in {"planner", "reviewer", "arbitrator", "generator_revision", "repair_plan"}:
        assert "json" in prompt(name, "en").lower(), name


def test_load_settings_reads_config_adjacent_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("EVAL_ADJACENT_KEY", raising=False)
    (tmp_path / ".env").write_text("EVAL_ADJACENT_KEY=adjacent-secret\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        "llm:\n"
        "  model: test-model\n"
        "  api_key: ${EVAL_ADJACENT_KEY}\n",
        encoding="utf-8",
    )

    settings = load_settings(str(config))

    assert settings.llm.resolved_key() == "adjacent-secret"
