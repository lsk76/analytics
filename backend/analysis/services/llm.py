"""Async OpenRouter (OpenAI-compatible) client + JSON helpers."""
import json
import logging
from typing import Any, List, Optional

from django.conf import settings
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


def make_client() -> AsyncOpenAI:
    """One client per asyncio.run — pass it to many query() calls, then close it."""
    return AsyncOpenAI(
        api_key=settings.OPENROUTER_API_KEY,
        base_url=settings.OPENROUTER_API_BASE_URL,
    )


async def query(messages: List[dict], model: Optional[str] = None, timeout: float = 120.0,
                client: Optional[AsyncOpenAI] = None) -> str:
    model = model or settings.LLM_MODEL
    own = client is None
    if own:
        client = make_client()
    try:
        resp = await client.chat.completions.create(
            model=model, messages=messages, timeout=timeout,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM error (%s): %s", model, e)
        return ""
    finally:
        if own:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass


def extract_json(text: str) -> Any:
    """Pull a JSON array/object out of a model reply (tolerates ``` fences)."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    for op, cl in (("[", "]"), ("{", "}")):
        a, b = text.find(op), text.rfind(cl)
        if a != -1 and b != -1 and b > a:
            try:
                return json.loads(text[a:b + 1])
            except Exception:  # noqa: BLE001
                continue
    return None
