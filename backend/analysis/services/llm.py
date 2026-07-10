"""Async OpenRouter (OpenAI-compatible) client + JSON helpers."""
import asyncio
import json
import logging
from typing import Any, List, Optional

from django.conf import settings
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


def make_client() -> AsyncOpenAI:
    """One client per asyncio.run — pass it to many query() calls, then close it.

    max_retries=0: the SDK's default (2) silently triples the wall-clock of a bad
    call (each attempt waits the full timeout) and, on a flapping VPN, made dedup
    workers appear wedged for minutes. Callers tolerate a "" reply and retry on the
    next tick, so failing fast is strictly better than retrying inside a hung socket."""
    return AsyncOpenAI(
        api_key=settings.OPENROUTER_API_KEY,
        base_url=settings.OPENROUTER_API_BASE_URL,
        max_retries=0,
    )


async def query(messages: List[dict], model: Optional[str] = None, timeout: float = 120.0,
                client: Optional[AsyncOpenAI] = None,
                max_tokens: int = 2000,
                json_mode: bool = False) -> str:
    """Send a chat completion. `max_tokens` defaults to 2000 — large enough for
    classify batches and audit verdicts, while avoiding OpenRouter's 402 error
    ("requested up to N tokens but can only afford M") that fires when the model's
    *theoretical* max (e.g. 65535 for Gemini 2.5 Flash) is reserved against the
    key's monthly limit. Override per call for known-larger outputs.

    `json_mode=True` asks OpenRouter to enforce a strict JSON-object output
    (`response_format={"type":"json_object"}`). Use for callers that PARSE the
    reply with `extract_json`; otherwise the model sometimes returns a bare word
    ("напад", "бійка") that we then have to fall back on heuristically."""
    model = model or settings.LLM_MODEL
    own = client is None
    if own:
        client = make_client()
    try:
        kwargs = dict(model=model, messages=messages, timeout=timeout,
                      max_tokens=max_tokens)
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        # Hard asyncio ceiling ON TOP of the SDK's own `timeout`: a VPN that drops
        # mid-request can leave httpx blocked on a half-open socket where the SDK
        # read-timeout never fires — wait_for cancels the coroutine so a single bad
        # call can NEVER wedge the worker. +10s lets the SDK timeout win normally.
        resp = await asyncio.wait_for(
            client.chat.completions.create(**kwargs), timeout=timeout + 10)
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001 — incl. asyncio.TimeoutError
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
    # Whole text first: json_mode returns pure JSON. Bracket-hunting below tries
    # "[…]" BEFORE "{…}", so an OBJECT whose last structure is a single array
    # (e.g. {"tags":{"x":["a","b"]}}) would wrongly yield that inner ARRAY. Trying
    # the full string first returns the real object/array and is strictly safer.
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    for op, cl in (("[", "]"), ("{", "}")):
        a, b = text.find(op), text.rfind(cl)
        if a != -1 and b != -1 and b > a:
            try:
                return json.loads(text[a:b + 1])
            except Exception:  # noqa: BLE001
                continue
    return None
