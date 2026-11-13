"""Published OpenAI model metadata, verified 2026-10-26.

Only canonical names are maintained. Each entry is sourced from:
https://developers.openai.com/api/docs/models/{model}

Values are the published total context window and maximum output.
Runtime subtracts its configured output reserve from the context window.
Unknown names require an explicit context limit.
"""

# model: (context window, maximum output tokens)
_MODELS = {
    'gpt-4.1': (1047576, 32768),
    'gpt-4.1-mini': (1047576, 32768),
    'gpt-4.1-nano': (1047576, 32768),
    'gpt-4o': (128000, 16384),
    'gpt-4o-mini': (128000, 16384),
    'gpt-5': (400000, 128000),
    'gpt-5-mini': (400000, 128000),
    'gpt-5-nano': (400000, 128000),
    'gpt-5.1': (400000, 128000),
    'gpt-5.2': (400000, 128000),
    'gpt-5.4': (1050000, 128000),
    'gpt-5.4-mini': (400000, 128000),
    'gpt-5.4-nano': (400000, 128000),
    'gpt-5.5': (1050000, 128000),
    'gpt-6-astra': (1050000, 128000),
    'gpt-6-sol': (1050000, 128000),
    'gpt-6-luna': (1050000, 128000),
    'gpt-6.1-sol': (1050000, 128000),
}


def model_name(model: str) -> str | None:
    route, separator, name = model.partition("/")
    return name if route == "openai" and separator else model if not separator else None


def model_info(model: str) -> dict[str, object]:
    values = _MODELS.get(model)
    if values is None:
        return {}
    context, output = values
    return {"max_input_tokens": context, "max_output_tokens": output}

