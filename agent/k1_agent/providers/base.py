"""
Provider-neutral chat interface, so the agent can switch between Claude, OpenAI GPT-5.x and
GPT-6 Astra (an OpenAI model) and Google Gemini by config.

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
class Usage:
    """Tokens billed for one model call. `input` excludes cached tokens; `output` includes reasoning."""
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0      # the part of `output` spent thinking, when the provider reports it

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(*(a + b for a, b in zip(self.as_tuple(), other.as_tuple())))

    def as_tuple(self) -> tuple:
        return self.input, self.output, self.cache_read, self.cache_write, self.reasoning

    def as_dict(self) -> dict:
        return {"input": self.input, "output": self.output, "cache_read": self.cache_read,
                "cache_write": self.cache_write, "reasoning": self.reasoning}

    @property
    def prompt_tokens(self) -> int:
        """Everything the model read for this call, cached or not (decides long-context pricing)."""
        return self.input + self.cache_read + self.cache_write


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: list = field(default_factory=list)     # provider-specific output items, replayed in later turns
    usage: Usage | None = None
    model: str | None = None                    # the model id the provider says answered


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
    if name == "gemini":
        from k1_agent.providers.gemini import GeminiProvider
        return GeminiProvider(model, **kwargs)
    raise ValueError(f"unknown provider {name!r}, use 'anthropic', 'openai' or 'gemini'")
