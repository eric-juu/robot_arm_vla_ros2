import json
from types import SimpleNamespace as NS

from k1_agent.providers.anthropic import AnthropicProvider
from k1_agent.providers.base import ToolSpec, image_part, make_provider, text_part
from k1_agent.providers.openai import OpenAIProvider

TOOLS = [ToolSpec("observe", "look", {"type": "object", "properties": {}})]


class Recorder:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def conversation(first_reply):
    return [
        {"role": "user", "content": [text_part("task"), image_part(b"\xff\xd8")]},
        {"role": "assistant", "reply": first_reply},
        {"role": "tool", "results": [{"id": "call_1", "name": "observe",
                                      "content": [text_part('{"ok": true}'), image_part(b"\xff\xd8")]}]},
        {"role": "user", "content": [text_part("continue")]},
    ]


def test_anthropic_round_trip():
    response = NS(content=[NS(type="text", text="Looking."),
                           NS(type="tool_use", id="call_1", name="observe", input={})])
    messages_api = Recorder(response)
    provider = AnthropicProvider("claude-test", client=NS(messages=messages_api))
    reply = provider.chat("sys", [{"role": "user", "content": [text_part("task")]}], TOOLS)
    assert reply.text == "Looking." and reply.tool_calls[0].name == "observe"
    assert messages_api.kwargs["tools"][0]["input_schema"] == {"type": "object", "properties": {}}

    provider.chat("sys", conversation(reply), [])
    sent = messages_api.kwargs["messages"]
    assert "tools" not in messages_api.kwargs
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]       # tool result + text merged
    assert sent[0]["content"][1]["source"]["media_type"] == "image/jpeg"
    assert sent[1]["content"][1] == {"type": "tool_use", "id": "call_1", "name": "observe", "input": {}}
    result, follow_up = sent[2]["content"]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "call_1"
    assert result["content"][1]["type"] == "image" and follow_up == {"type": "text", "text": "continue"}


def test_openai_round_trip():
    item = NS(model_dump=lambda exclude_none: {"type": "function_call", "call_id": "call_1", "name": "observe",
                                               "arguments": "{}", "id": "fc_1"})
    responses_api = Recorder(NS(output=[item], output_text=""))
    provider = OpenAIProvider("gpt-6-astra", client=NS(responses=responses_api), reasoning_effort="high")
    reply = provider.chat("sys", [{"role": "user", "content": [text_part("task")]}], TOOLS)
    assert reply.tool_calls[0].id == "call_1" and reply.tool_calls[0].arguments == {}
    assert responses_api.kwargs["model"] == "gpt-6-astra" and responses_api.kwargs["reasoning"] == {"effort": "high"}
    assert responses_api.kwargs["tools"][0]["type"] == "function"

    provider.chat("sys", conversation(reply), TOOLS)
    sent = responses_api.kwargs["input"]
    assert sent[0]["content"][1]["image_url"].startswith("data:image/jpeg;base64,")
    assert sent[1]["type"] == "function_call"
    assert sent[2] == {"type": "function_call_output", "call_id": "call_1", "output": json.dumps({"ok": True})}
    assert sent[3]["role"] == "user" and sent[3]["content"][1]["type"] == "input_image"     # tool frames
    assert sent[4]["content"] == [{"type": "input_text", "text": "continue"}]


def test_make_provider_rejects_unknown():
    try:
        make_provider("gemini", "x")
    except ValueError as e:
        assert "unknown provider" in str(e)
    else:
        raise AssertionError("expected ValueError")
