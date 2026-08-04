from __future__ import annotations

import json
import re

from ..config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

# Deliberately narrow. This is not a chat assistant -- it does one thing that
# targets task-initiation paralysis: turn a task too vague/big to start into
# concrete steps whose first one is small enough to not trigger avoidance.
_BREAKDOWN_PROMPT = """You break tasks into starter steps for someone with ADHD who procrastinates.

Rules:
- 3 to 5 steps, in order.
- The FIRST step must take under 2 minutes and be physical/concrete (open the file, find the phone number, put the shoes by the door). It exists purely to break inertia.
- Every step is an observable action, never "think about" or "plan" or "consider".
- Max 10 words per step. No numbering, no preamble, no encouragement.

Return ONLY a JSON array of strings. Example: ["Open the doc", "Write one bad sentence", "Set a 10 min timer"]"""


class LLMError(RuntimeError):
    pass


def llm_enabled() -> bool:
    return bool(LLM_API_KEY)


def _extract_json_array(raw: str) -> list[str]:
    """Small models wrap JSON in prose or code fences often enough that parsing
    the bare string is unreliable."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            raise LLMError("model did not return a JSON array")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as e:
            raise LLMError(f"could not parse model output: {e}") from e

    if not isinstance(parsed, list):
        raise LLMError("model output was not a list")

    steps = [str(s).strip() for s in parsed if str(s).strip()]
    if not steps:
        raise LLMError("model returned no steps")
    return steps[:5]


def break_down_task(task_text: str) -> list[str]:
    if not llm_enabled():
        raise LLMError("No LLM key set. Add LLM_API_KEY (OpenRouter free tier works).")

    from openai import OpenAI

    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": _BREAKDOWN_PROMPT},
                {"role": "user", "content": f"Task: {task_text}"},
            ],
            temperature=0.4,
            max_tokens=300,
        )
    except Exception as e:
        raise LLMError(str(e)) from e

    content = (resp.choices[0].message.content or "") if resp.choices else ""
    return _extract_json_array(content)
