"""
Provider-neutral chat interface, so the agent can switch between Claude, OpenAI GPT-5.x and
GPT-6 Astra (an OpenAI model) by config.

Conversation messages are plain dicts:
    {"role": "user", "content": [Part, ...]}
    {"role": "assistant", "reply": Reply}                       a previous model turn, replayed as-is
    {"role": "tool", "results": [{"id", "name", "content": [Part, ...]}, ...]}
where a Part is {"type": "text", "text": str} or {"type": "image", "jpeg": bytes}.
"""
from dataclasses import dataclass, field


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict        # JSON schema of the arguments object


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: list = field(default_factory=list)     # provider-specific output items, replayed in later turns


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def image_part(jpeg: bytes) -> dict:
    return {"type": "image", "jpeg": jpeg}


class Provider:
    name = "base"

    def __init__(self, model: str):
        self.model = model

    def chat(self, system: str, messages: list[dict], tools: list[ToolSpec], max_tokens: int = 4096) -> Reply:
        raise NotImplementedError


def make_provider(name: str, model: str, **kwargs) -> Provider:
    if name == "anthropic":
        from k1_agent.providers.anthropic import AnthropicProvider
        return AnthropicProvider(model, **kwargs)
    if name == "openai":
        from k1_agent.providers.openai import OpenAIProvider
        return OpenAIProvider(model, **kwargs)
    raise ValueError(f"unknown provider {name!r}, use 'anthropic' or 'openai'")
