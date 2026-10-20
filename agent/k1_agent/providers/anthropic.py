"""Claude through the Anthropic Messages API (tool use + images). Key: ANTHROPIC_API_KEY."""
import base64

from k1_agent.providers.base import Provider, Reply, ToolCall, ToolSpec


def to_blocks(parts: list[dict]) -> list[dict]:
    blocks = []
    for part in parts:
        if part["type"] == "text":
            blocks.append({"type": "text", "text": part["text"]})
        elif part["type"] == "image":
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                       "data": base64.b64encode(part["jpeg"]).decode()}})
    return blocks


def to_messages(messages: list[dict]) -> list[dict]:
    out = []

    def add(role, content):
        # tool results are user turns too; consecutive user turns are merged, results first
        if out and out[-1]["role"] == role == "user":
            out[-1]["content"].extend(content)
        else:
            out.append({"role": role, "content": content})

    for msg in messages:
        if msg["role"] == "user":
            add("user", to_blocks(msg["content"]))
        elif msg["role"] == "assistant":
            add("assistant", list(msg["reply"].raw))
        elif msg["role"] == "tool":
            add("user", [{"type": "tool_result", "tool_use_id": r["id"], "content": to_blocks(r["content"])}
                         for r in msg["results"]])
    return out


def block_to_dict(block) -> dict:
    kind = block.type
    if kind == "text":
        return {"type": "text", "text": block.text}
    if kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if kind == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    return block.model_dump(exclude_none=True)


def from_response(response) -> Reply:
    raw = [block_to_dict(b) for b in response.content]
    text = "\n".join(b["text"] for b in raw if b["type"] == "text")
    calls = [ToolCall(b["id"], b["name"], dict(b["input"] or {})) for b in raw if b["type"] == "tool_use"]
    return Reply(text, calls, raw)


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str, client=None, api_key: str | None = None):
        super().__init__(model)
        if client is None:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
        self.client = client

    def chat(self, system: str, messages: list[dict], tools: list[ToolSpec], max_tokens: int = 4096) -> Reply:
        kwargs = {}
        if tools:
            kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
                               for t in tools]
        response = self.client.messages.create(model=self.model, max_tokens=max_tokens, system=system,
                                               messages=to_messages(messages), **kwargs)
        return from_response(response)
