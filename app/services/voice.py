from __future__ import annotations

import io

from ..config import OPENAI_API_KEY, OPENAI_TRANSCRIBE_MODEL


def voice_enabled() -> bool:
    return bool(OPENAI_API_KEY)


def transcribe_ogg(audio_bytes: bytes) -> str:
    """Transcribes a Telegram voice note (OGG/Opus) to text via OpenAI Whisper."""
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY)
    buf = io.BytesIO(audio_bytes)
    buf.name = "voice.ogg"
    result = client.audio.transcriptions.create(model=OPENAI_TRANSCRIBE_MODEL, file=buf)
    return (result.text or "").strip()
