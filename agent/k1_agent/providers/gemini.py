"""
Google Gemini (for example gemini-4-argon) through the google-genai SDK: function calling + images.
Key: GEMINI_API_KEY (or GOOGLE_API_KEY).

The model's own turn is replayed exactly as returned (a types.Content), because Gemini's thinking models
attach thought signatures to it that must come back unchanged.
"""
from k1_agent.providers.base import Provider, Reply, ToolCall, ToolSpec, Usage

GENERATED_ID = "gemini_call_"     # ids we make up when Gemini doesn't give its function calls one


def _types():
    from google.genai import types
    return types


def to_parts(parts: list[dict]) -> list:
    types = _types()
    out = []
    for part in parts:
        if part["type"] == "text":
            out.append(types.Part.from_text(text=part["text"]))
        elif part["type"] == "image":
            out.append(types.Part.from_bytes(data=part["jpeg"], mime_type="image/jpeg"))
    return out


def to_contents(messages: list[dict]) -> list:
    types = _types()
    contents = []

    def add(role, parts):
        if contents and contents[-1].role == role == "user":
            contents[-1].parts.extend(parts)
        else:
            contents.append(types.Content(role=role, parts=parts))

    for msg in messages:
        if msg["role"] == "user":
            add("user", to_parts(msg["content"]))
        elif msg["role"] == "assistant":
            contents.extend(msg["reply"].raw)
        elif msg["role"] == "tool":
            parts, images = [], []
            for r in msg["results"]:
                text = "\n".join(p["text"] for p in r["content"] if p["type"] == "text")
                call_id = None if r["id"].startswith(GENERATED_ID) else r["id"]
                parts.append(types.Part(function_response=types.FunctionResponse(
                    id=call_id, name=r["name"], response={"output": text})))
                images.extend(p for p in r["content"] if p["type"] == "image")
            # function responses carry text; the frames the tools returned follow in the same turn
            if images:
                parts += to_parts([{"type": "text", "text": "Camera frames returned by the tool calls above:"}]
                                  + images)
            add("user", parts)
    return contents


def from_response(response) -> Reply:
    types = _types()
    candidate = (response.candidates or [None])[0]
    content = getattr(candidate, "content", None)
    if content is None or not content.parts:
        # blocked or empty: keep a placeholder turn so the conversation stays well formed
        content = types.Content(role="model", parts=[types.Part.from_text(text="(no reply)")])
    texts, calls = [], []
    for part in content.parts:
        if part.function_call is not None:
            call = part.function_call
            calls.append(ToolCall(call.id or f"{GENERATED_ID}{len(calls) + 1}", call.name, dict(call.args or {})))
        elif part.text and not part.thought:
            texts.append(part.text)
    return Reply("\n".join(texts), calls, [content], usage_of(getattr(response, "usage_metadata", None)),
                 getattr(response, "model_version", None))


def usage_of(meta) -> Usage | None:
    if meta is None:
        return None
    prompt = meta.prompt_token_count or 0
    cached = meta.cached_content_token_count or 0
    thoughts = meta.thoughts_token_count or 0
    # Gemini bills thinking tokens as output; prompt_token_count includes the cached part
    return Usage(input=prompt - cached, output=(meta.candidates_token_count or 0) + thoughts,
                 cache_read=cached, reasoning=thoughts)


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, model: str, client=None, api_key: str | None = None):
        super().__init__(model)
        if client is None:
            from google import genai
            client = genai.Client(api_key=api_key) if api_key else genai.Client()
        self.client = client

    def chat(self, system: str, messages: list[dict], tools: list[ToolSpec], max_tokens: int = 4096) -> Reply:
        types = _types()
        config = {"system_instruction": system, "max_output_tokens": max_tokens,
                  # the agent loop runs the tools itself
                  "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True)}
        if tools:
            config["tools"] = [types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
                for t in tools])]
        response = self.client.models.generate_content(model=self.model, contents=to_contents(messages),
                                                       config=types.GenerateContentConfig(**config))
        return from_response(response)
