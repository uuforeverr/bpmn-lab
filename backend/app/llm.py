from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI

from .config import LlmConfig
from .domain import EditCall
from .tools import EDIT_TOOLS


@dataclass
class LlmResult:
    content: str | None
    tool_calls: list[EditCall]
    usage: dict
    latency_ms: int
    raw: dict
    finish_reason: str | None = None
    thinking_mode: str | None = None


class LlmClient:
    def __init__(self, config: LlmConfig):
        self.config = config
        self.client = OpenAI(api_key=config.resolved_key(), base_url=config.base_url,
                             timeout=config.timeout_seconds, max_retries=config.max_transport_retries)

    def complete(self, messages: list[dict], tools: bool = False, repair: bool = False,
                 agent: str = "") -> LlmResult:
        started = time.perf_counter()
        thinking_mode = self.config.thinking_for(agent)
        allowed = EDIT_TOOLS if repair else [tool for tool in EDIT_TOOLS if tool["function"]["name"] in {
            "add_task", "add_event", "add_gateway", "add_edge", "add_linear_sequence",
        }]
        kwargs = self._request_kwargs(messages, agent, thinking_mode)
        if tools:
            kwargs["tools"] = allowed
            if self.config.tool_choice is not None:
                kwargs["tool_choice"] = self.config.tool_choice
            if self.config.parallel_tool_calls is not None:
                kwargs["parallel_tool_calls"] = self.config.parallel_tool_calls
        elif self.config.json_response_format:
            kwargs["response_format"] = {"type": "json_object"}
        response = self.client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        message = choice.message
        calls = [EditCall(name=item.function.name, arguments=json.loads(item.function.arguments)) for item in (message.tool_calls or [])]
        return LlmResult(message.content, calls, response.usage.model_dump() if response.usage else {},
                         int((time.perf_counter() - started) * 1000), response.model_dump(),
                         choice.finish_reason, thinking_mode)

    def _request_kwargs(self, messages: list[dict], agent: str,
                        thinking_mode: str | None) -> dict:
        """Build a Chat Completions request without assuming provider extensions."""
        kwargs: dict = {"model": self.config.model, "messages": messages}
        if self.config.temperature is not None:
            kwargs["temperature"] = self.config.temperature
        if self.config.max_tokens_parameter is not None:
            kwargs[self.config.max_tokens_parameter] = self.config.max_output_tokens

        extra_body = deepcopy(self.config.extra_body)
        extra_body.update(deepcopy(self.config.extra_body_by_agent.get(agent, {})))
        if thinking_mode and self.config.thinking_parameter:
            extra_body[self.config.thinking_parameter] = deepcopy(
                self.config.thinking_value(thinking_mode)
            )
        if extra_body:
            kwargs["extra_body"] = extra_body
        return kwargs


def parse_json_object(content: str | None, agent: str) -> dict:
    """Accept a JSON object even when a model wraps it in a Markdown fence."""
    if not content or not content.strip():
        raise ValueError(f"{agent} returned empty content")
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise ValueError(f"{agent} did not return a JSON object") from None
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError as exc:
            raise ValueError(f"{agent} returned invalid JSON: {exc.msg}") from None
    if not isinstance(value, dict):
        raise ValueError(f"{agent} must return a JSON object")
    return value


def prompt(name: str) -> str:
    return (Path(__file__).parents[1] / "prompts" / f"{name}.md").read_text("utf-8")
