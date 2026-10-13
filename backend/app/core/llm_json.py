"""A model's answer as one validated JSON object (M39 F3, D228): the first {...} block in the reply, checked with a
pydantic model; one retry that shows the model what was wrong; None after that. LLMError/LLMUnavailable propagate."""

import json

from pydantic import BaseModel, ValidationError

RETRY = "\n\nYour last reply was not valid: {error}\nReply again with only the JSON object."


def _first_object(text: str) -> str | None:
    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for end in range(start, len(text)):
            ch = text[end]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : end + 1]
        start = text.find("{", start + 1)
    return None


def _parse(text: str, model: type[BaseModel]) -> tuple[BaseModel | None, str | None]:
    block = _first_object(text)
    if block is None:
        return None, "no JSON object found"
    try:
        return model.model_validate(json.loads(block)), None
    except (json.JSONDecodeError, ValidationError) as exc:
        return None, str(exc)[:500]


async def ask_json(llm, system: str, prompt: str, model: type[BaseModel]) -> BaseModel | None:
    reply = "".join([token async for token in llm.stream(system, prompt)])
    parsed, error = _parse(reply, model)
    if parsed is not None:
        return parsed
    retry = "".join([token async for token in llm.stream(system, prompt + RETRY.format(error=error))])
    return _parse(retry, model)[0]
