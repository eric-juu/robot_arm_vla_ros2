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
        make_provider("llama", "x")
    except ValueError as e:
        assert "unknown provider" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_usage_is_read_from_each_provider():
    from k1_agent.providers.anthropic import usage_of as anthropic_usage
    from k1_agent.providers.openai import usage_of as openai_usage

    u = anthropic_usage(NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=1000,
                           cache_creation_input_tokens=None))
    assert u.as_dict() == {"input": 100, "output": 20, "cache_read": 1000, "cache_write": 0, "reasoning": 0}
    u = openai_usage(NS(input_tokens=1100, output_tokens=50, input_tokens_details=NS(cached_tokens=1000),
                        output_tokens_details=NS(reasoning_tokens=30)))
    assert (u.input, u.cache_read, u.output, u.reasoning) == (100, 1000, 50, 30)


def test_gemini_round_trip():
    import pytest
    types = pytest.importorskip("google.genai.types")
    from k1_agent.providers.gemini import GeminiProvider

    model_turn = types.Content(role="model", parts=[
        types.Part(text="thinking...", thought=True),
        types.Part.from_text(text="Looking."),
        types.Part(function_call=types.FunctionCall(name="observe", args={}), thought_signature=b"sig")])
    response = types.GenerateContentResponse(
        candidates=[types.Candidate(content=model_turn)], model_version="gemini-4-argon",
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=1200, cached_content_token_count=1000, candidates_token_count=40,
            thoughts_token_count=60))
    models_api = Recorder(response)
    models_api.generate_content = models_api.create
    provider = GeminiProvider("gemini-4-argon", client=NS(models=models_api))

    reply = provider.chat("sys", [{"role": "user", "content": [text_part("task")]}], TOOLS)
    assert reply.text == "Looking."             # the thought part is not shown as text
    assert reply.tool_calls[0].name == "observe" and reply.tool_calls[0].id.startswith("gemini_call_")
    assert (reply.usage.input, reply.usage.cache_read, reply.usage.output, reply.usage.reasoning) == (200, 1000, 100, 60)
    assert reply.model == "gemini-4-argon"
    config = models_api.kwargs["config"]
    assert config.system_instruction == "sys" and config.automatic_function_calling.disable
    assert config.tools[0].function_declarations[0].name == "observe"

    tool_turn = {"role": "tool", "results": [{"id": reply.tool_calls[0].id, "name": "observe",
                                              "content": [text_part('{"ok": true}'), image_part(b"\xff\xd8")]}]}
    provider.chat("sys", conversation(reply)[:2] + [tool_turn, {"role": "user", "content": [text_part("continue")]}],
                  [])
    sent = models_api.kwargs["contents"]
    assert [c.role for c in sent] == ["user", "model", "user"]
    assert sent[1] is model_turn                                          # replayed unchanged (thought signature)
    response_part, label, frame, follow_up = sent[2].parts
    assert response_part.function_response.name == "observe" and response_part.function_response.id is None
    assert response_part.function_response.response == {"output": '{"ok": true}'}
    assert frame.inline_data.mime_type == "image/jpeg" and follow_up.text == "continue"
    assert "tools" not in models_api.kwargs["config"].model_dump(exclude_none=True)
