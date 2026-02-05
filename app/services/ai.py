from __future__ import annotations
import os

AI_ENABLED = os.environ.get("AI_FOLLOWUPS", "0") == "1"

def ai_enabled() -> bool:
    return AI_ENABLED and bool(os.environ.get("OPENAI_API_KEY"))

async def generate_followup(prompt: str) -> str:
    """
    Uses OpenAI Responses API to generate a short follow-up message.
    Falls back to a simple template if disabled.
    """
    if not ai_enabled():
        return prompt

    # OpenAI Responses API (recommended; Assistants is deprecated). :contentReference[oaicite:2]{index=2}
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

    resp = await client.responses.create(
        model=model,
        input=(
            "You are a gentle, concise scheduling assistant. "
            "Write ONE short message (max 2 sentences), friendly, no emojis. "
            "End by asking for a photo.\n\n"
            f"Context: {prompt}"
        ),
    )
    # Responses API returns output text via output_text helper in many SDKs; be safe:
    try:
        return resp.output_text
    except Exception:
        # fallback
        return prompt
