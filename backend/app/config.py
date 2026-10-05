from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

ThinkingMode = Literal["enabled", "disabled"]


class AppConfig(BaseModel):
    database_path: str = "data/bpmn_lab.sqlite3"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])


class LlmConfig(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    api_key: str = "${OPENAI_API_KEY}"
    model: str = ""
    temperature: float | None = 0
    timeout_seconds: float = 120
    max_transport_retries: int = 2
    max_output_tokens: int = 8192
    max_tokens_parameter: Literal["max_tokens", "max_completion_tokens"] | None = "max_tokens"
    json_response_format: bool = True
    parallel_tool_calls: bool | None = True
    tool_choice: str | None = "auto"
    thinking: ThinkingMode | None = None
    thinking_by_agent: dict[str, ThinkingMode] = Field(default_factory=dict)
    thinking_parameter: str | None = "thinking"
    thinking_enabled_value: Any = Field(default_factory=lambda: {"type": "enabled"})
    thinking_disabled_value: Any = Field(default_factory=lambda: {"type": "disabled"})
    extra_body: dict[str, Any] = Field(default_factory=dict)
    extra_body_by_agent: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def resolved_key(self) -> str:
        match = re.fullmatch(r"\$\{([A-Z0-9_]+)\}", self.api_key)
        return os.environ.get(match.group(1), "") if match else self.api_key

    def thinking_for(self, agent: str) -> ThinkingMode | None:
        return self.thinking_by_agent.get(agent, self.thinking)

    def thinking_value(self, mode: ThinkingMode) -> Any:
        return self.thinking_enabled_value if mode == "enabled" else self.thinking_disabled_value


class LayoutConfig(BaseModel):
    enabled: bool = False
    base_url: str = "http://127.0.0.1:3001"
    timeout_seconds: float = 15


class PipelineConfig(BaseModel):
    max_semantic_repairs: int = 5
    fixed_length_sentences: int = 3
    max_agent_retries: int = 2
    reviewer_enabled: bool = False


class Settings(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    layout: LayoutConfig = Field(default_factory=LayoutConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)


def load_settings(path: str | None = None) -> Settings:
    load_dotenv(Path(".env"), override=False)
    target = Path(path or os.getenv("BPMN_LAB_CONFIG", "config.yaml"))
    if not target.exists():
        example = Path("config.example.yaml")
        target = example if example.exists() else target
    data: dict[str, Any] = yaml.safe_load(target.read_text("utf-8")) if target.exists() else {}
    return Settings.model_validate(data or {})
