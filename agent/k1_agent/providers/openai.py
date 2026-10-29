"""
OpenAI models through the Responses API (function calling + images). Key: OPENAI_API_KEY.
GPT-5.x works today; GPT-6 Astra is the same API with a different model name once you have access.
"""
import base64
import json

from k1_agent.providers.base import Provider, Reply, ToolCall, ToolSpec, Usage


def to_content(parts: list[dict]) -> list[dict]:
    content = []
    for part in parts:
        if part["type"] == "text":
            content.append({"type": "input_text", "text": part["text"]})
        elif part["type"] == "image":
            url = "data:image/jpeg;base64," + base64.b64encode(part["jpeg"]).decode()
            content.append({"type": "input_image", "image_url": url})
    return content


def to_input(messages: list[dict]) -> list[dict]:
    items = []
    for msg in messages:
        if msg["role"] == "user":
            items.append({"role": "user", "content": to_content(msg["content"])})
        elif msg["role"] == "assistant":
            items.extend(msg["reply"].raw)
        elif msg["role"] == "tool":
            images = []
            for r in msg["results"]:
                text = "\n".join(p["text"] for p in r["content"] if p["type"] == "text")
                items.append({"type": "function_call_output", "call_id": r["id"], "output": text})
                images.extend(p for p in r["content"] if p["type"] == "image")
            # function outputs carry text only; frames the tools returned follow as a user message
            if images:
                items.append({"role": "user", "content": to_content(
                    [{"type": "text", "text": "Camera frames returned by the tool calls above:"}] + images)})
    return items


def from_response(response) -> Reply:
    raw, calls = [], []
    for item in response.output:
        data = item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else dict(item)
        raw.append(data)
        if data.get("type") == "function_call":
            calls.append(ToolCall(data["call_id"], data["name"], json.loads(data.get("arguments") or "{}")))
    return Reply(getattr(response, "output_text", "") or "", calls, raw,
                 usage_of(getattr(response, "usage", None)), getattr(response, "model", None))


def usage_of(usage) -> Usage | None:
    if usage is None:
        return None
    details = getattr(usage, "input_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) or 0
    reasoning = getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", None) or 0
    # input_tokens counts cached tokens too; OpenAI bills them at the cached rate
    return Usage(input=(usage.input_tokens or 0) - cached, output=usage.output_tokens or 0,
                 cache_read=cached, reasoning=reasoning)


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, model: str, client=None, api_key: str | None = None, reasoning_effort: str | None = None):
        super().__init__(model)
        if client is None:
            import openai
            client = openai.OpenAI(api_key=api_key)
        self.client = client
        self.reasoning_effort = reasoning_effort

    def chat(self, system: str, messages: list[dict], tools: list[ToolSpec], max_tokens: int = 4096) -> Reply:
        kwargs = {}
        if self.reasoning_effort:
            kwargs["reasoning"] = {"effort": self.reasoning_effort}
        if tools:
            kwargs["tools"] = [{"type": "function", "name": t.name, "description": t.description,
                                "parameters": t.parameters, "strict": False} for t in tools]
        response = self.client.responses.create(model=self.model, instructions=system, input=to_input(messages),
                                                max_output_tokens=max_tokens, **kwargs)
        return from_response(response)
